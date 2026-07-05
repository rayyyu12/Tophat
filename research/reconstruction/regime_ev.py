"""EV per ticket if each observed regime PERSISTED, raw and bias-corrected.

Bias correction: per-bracket delta = (docs 1s full-period WR) - (minute
reconstruction full-period WR), added to each regime WR. Assumes the
minute-vs-1s bias is constant across months.
"""
from __future__ import annotations

import json

import numpy as np

import mc_lifecycle as mc

KEYMAP = {
    "ts_eval": "ts_eval_15_10", "ts_nuke": "ts_nuke_80_25",
    "ts_renuke": "ts_renuke_105_25", "ts_flip": "ts_flip_85_50",
    "ax_eval1": "ax_eval_30_10", "ax_eval2": "ax_eval2_40_10",
    "ax_nuke": "ax_nuke_325_25", "ax_flip": "ax_flip_1625_50",
}
DOCS_FULL = {"ts_eval": 0.466, "ts_nuke": 0.279, "ts_renuke": 0.236,
             "ts_flip": 0.884, "ax_eval1": 0.302, "ax_eval2": 0.235,
             "ax_nuke": 0.488, "ax_flip": 0.815}

wrs = json.load(open("regime_wrs.json"))
recon_full = {k: wrs["FULL"][v] for k, v in KEYMAP.items()}
bias = {k: DOCS_FULL[k] - recon_full[k] for k in KEYMAP}
print("bias correction (pp):", {k: f"{v*100:+.1f}" for k, v in bias.items()})

N = 60_000
results = {}
print(f"\n{'regime':16s} {'':10s} {'TS EV':>8s} {'TS pass':>8s} {'AX EV':>8s} {'AX pass':>8s}")
for reg in ["Jun-Aug25", "Sep-Oct25", "Nov25-Feb26", "Mar-May26"]:
    for corrected in (False, True):
        pvec = {}
        for k, v in KEYMAP.items():
            p = wrs[reg][v] + (bias[k] if corrected else 0.0)
            pvec[k] = (float(np.clip(p, 0.01, 0.99)),) * 2
        mc.P = pvec
        rt = mc.run(mc.topstep_ticket, 0.0, N)
        ra = mc.run(mc.apex_ticket, 0.0, N)
        tag = "corrected" if corrected else "raw"
        results[f"{reg}|{tag}"] = {"ts": rt, "ax": ra}
        print(f"{reg:16s} {tag:10s} {rt['ev']:+8.0f} {rt['pass']:8.1%} "
              f"{ra['ev']:+8.0f} {ra['pass']:8.1%}")

json.dump(results, open("regime_ev.json", "w"))
