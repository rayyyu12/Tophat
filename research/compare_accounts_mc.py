"""End-to-end 50k vs 150k comparison: eval -> funded(recovery nuke) -> per-ticket EV.
Nuke day1/day2 win rates are MEASURED from consecutive drive-day pairs.
"""
from __future__ import annotations
import sys
from dataclasses import dataclass
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path: sys.path.insert(0, str(_ROOT))
import numpy as np
from research.backtest_common import Bracket, collect_drive_entries, load_days, resolve, run_drive

DAYS = load_days()
ENTRIES = collect_drive_entries(DAYS)

def flip_wr(target_pts, stop_pts):
    sg = run_drive(ENTRIES, Bracket("f", target_pts, stop_pts))
    n = sg["win"]+sg["loss"]; return sg["win"]/n if n else 0

def nuke_seq_wr(d1_tgt, d1_stp, d2_tgt, d2_stp):
    """Measure P(day1 win) and P(day2 win | day1 loss) from consecutive drive pairs."""
    e = ENTRIES
    d1_win = d1_n = 0
    d2_win = d2_n = 0
    i = 0
    while i < len(e)-1:
        day1, i1, dir1 = e[i]
        r1 = resolve(day1, i1, dir1, Bracket("d1", d1_tgt, d1_stp))
        if r1 == "win":
            d1_win += 1; d1_n += 1; i += 1; continue
        if r1 == "unresolved":
            i += 1; continue
        d1_n += 1  # loss
        day2, i2, dir2 = e[i+1]
        r2 = resolve(day2, i2, dir2, Bracket("d2", d2_tgt, d2_stp))
        if r2 in ("win","loss"):
            d2_n += 1
            if r2 == "win": d2_win += 1
        i += 2
    p1 = d1_win/d1_n if d1_n else 0
    p2 = d2_win/d2_n if d2_n else 0
    return p1, p2

@dataclass
class Acct:
    name: str; initial: float; trailing: float; dll: float
    funded_ct: int; eval_ct: int; pv: float
    eval_target: float; eval_tgt_pts: float; eval_stp_pts: float
    payout_cap: float; ticket: float
    nuke_dollars: float; recov_dollars: float  # day1 net target, day2 GROSS target

def eval_pass(rng, a):
    eq = a.initial; peak = eq
    win = a.eval_tgt_pts*a.eval_ct*a.pv
    win = min(a.eval_target/2, win)  # consistency cap
    loss = a.eval_stp_pts*a.eval_ct*a.pv
    p = a.eval_stp_pts/(a.eval_tgt_pts+a.eval_stp_pts) + 0.076  # measured eval edge
    days = 0
    while days < 30:
        days += 1
        if rng.random() < p: eq += win
        else: eq -= loss
        peak = max(peak, eq)
        if eq <= min(a.initial, peak - a.trailing): return False
        if eq - a.initial >= a.eval_target and days >= 2: return True
    return False

def funded(rng, a, p1, p2, flip, payouts_target=4):
    eq = a.initial; peak = eq; payouts = 0; withdrawn = 0.0; days = 0; nukes = 0
    flip_dollars = 150.0 if a.initial < 100000 else 150.0  # placeholder, set per acct below
    flip_dollars = a.flip_dollars
    def busted(): return eq <= min(a.initial, peak - a.trailing)
    while payouts < payouts_target and days < 200:
        wd = 0
        if payouts % 2 == 0:  # nuke cycle
            days += 1
            if rng.random() < p1:  # day1 win
                eq += a.nuke_dollars; nukes += 1; wd = 1
            else:
                eq -= a.dll; peak = max(peak, eq)
                if busted(): return withdrawn, payouts, nukes, True
                days += 1
                # day2 recovery: gross target = recov_dollars, stop = remaining room (<=dll)
                if rng.random() < p2:
                    eq += a.recov_dollars; nukes += 1; wd = 1
                else:
                    eq -= min(a.dll, peak - a.trailing - (eq - a.dll) if False else a.dll)
                    return withdrawn, payouts, nukes, True
            peak = max(peak, eq)
        while wd < 5:
            days += 1
            if rng.random() < flip: eq += flip_dollars; wd += 1
            else: eq -= a.dll
            peak = max(peak, eq)
            if busted(): return withdrawn, payouts, nukes, True
        profit = eq - a.initial
        if profit <= 0: break
        take = min(0.5*profit, a.payout_cap); withdrawn += take; eq -= take; payouts += 1
    return withdrawn, payouts, nukes, False

def run(a, p1, p2, flip, NF=40000):
    rng = np.random.default_rng(7)
    passes = np.mean([eval_pass(rng, a) for _ in range(NF)])
    res = [funded(rng, a, p1, p2, flip) for _ in range(NF)]
    w = np.array([r[0] for r in res]); po = np.array([r[1] for r in res])
    nk = np.array([r[2] for r in res]); bust = np.mean([r[3] for r in res])
    per_ticket = passes * w.mean() - a.ticket
    roi = per_ticket / a.ticket
    print(f"{a.name:<26} pass {passes:>5.1%} | p1 {p1:>5.1%} p2 {p2:>5.1%} flip {flip:.1%} | "
          f"$/acct ${w.mean():>6,.0f} pyts {po.mean():.2f} P1 {np.mean(po>=1):>4.0%} bust {bust:>4.0%} | "
          f"EV/ticket ${per_ticket:>+6,.0f} ROI {roi:>+5.0%}")
    return per_ticket, roi

print(f"Days {len(DAYS)} | drive entries {len(ENTRIES)}\n")
print("="*150)
# 50k: 2-mini funded ($40/pt), $1k DLL=25pt, $2k trail, $2k cap, $85 ticket, 5-mini eval
a50 = Acct("50k $3,200 nuke",50000,2000,1000,2,5,20, 3000,15.5,9.5, 2000,85, 3200, 4200)
a50.flip_dollars = 170.0
p1,p2 = nuke_seq_wr(80,25, 105,25); run(a50, p1,p2, flip_wr(8.5,50))

print("-"*150)
# 150k: 3-mini funded ($60/pt), $3k DLL=50pt, $4.5k trail, $5k cap, $195 ticket, 15-mini eval
def mk150(name, nuke_d, recov_d, nuke_tgt_pt, recov_tgt_pt):
    a = Acct(name,150000,4500,3000,3,15,20, 9000,15.5,9.5, 5000,195, nuke_d, recov_d)
    a.flip_dollars = 150.0
    p1,p2 = nuke_seq_wr(nuke_tgt_pt,50, recov_tgt_pt,25)
    run(a, p1,p2, flip_wr(2.5,50))

# nuke target -> pts at 3mini($60/pt); recovery gross = nuke + $3000 loss, at 25pt stop
mk150("150k $5,000 nuke",  5000, 8000,  83.33, 133.33)
mk150("150k $6,000 nuke",  6000, 9000, 100.0,  150.0)
mk150("150k $7,000 nuke",  7000,10000, 116.67, 166.67)
mk150("150k $9,000 nuke",  9000,12000, 150.0,  200.0)
mk150("150k $10,000 nuke",10000,13000, 166.67, 216.67)
print("="*150)
print("Note: 150k 'recovery' day2 risks only $1,500 (room left after a $3k day1 loss) -> 25pt stop, huge gross target.")
