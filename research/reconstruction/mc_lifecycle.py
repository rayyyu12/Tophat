"""Corrected intraday-MLL lifecycle Monte Carlo for Topstep 50K and Apex native,
parameterized by an edge multiplier lambda:

    p(bracket, lam) = clip(base + lam * (drive - base), 0.01, 0.99)

lam = 1 -> full documented drive edge; lam = 0 -> pure coin (driftless barrier
baseline, empirically proven); lam < 0 -> edge inverted. Near-floor tightened
stops recompute the barrier baseline and reapply the same lam-scaled pp edge.

Models follow docs/PROBABILITY.md (Topstep, corrected 2026-06-24) and
docs/MULTI_FIRM_PLAN.md section 3 (Apex native locked policy).
"""
from __future__ import annotations

import numpy as np

# (base, drive) win probabilities from the 251-day 1s cache (docs)
P = {
    "ts_eval":   (0.400, 0.466),   # 15/10pt @5m, +-(1500/1000)
    "ts_nuke":   (0.238, 0.279),   # 80/25 @2m, +3150/-1000
    "ts_renuke": (0.192, 0.236),   # 105/25 @2m, +4150/-1000
    "ts_flip":   (0.855, 0.884),   # 8.5/50 @1m, +150/-1000
    "ax_eval1":  (0.250, 0.302),   # 30/10 @5m
    "ax_eval2":  (0.200, 0.235),   # 40/10 @5m (reverse-engineered from pass=46.6%)
    "ax_nuke":   (0.435, 0.488),   # 32.5/25 @2m, +1250/-1000
    "ax_flip":   (0.755, 0.815),   # 16.25/50 @1m, +305/-1000
}


# theoretical driftless barrier baselines of the parent brackets
BARRIER = {
    "ts_eval": 10 / 25, "ts_nuke": 25 / 105, "ts_renuke": 25 / 130,
    "ts_flip": 50 / 58.5, "ax_eval1": 10 / 40, "ax_eval2": 10 / 50,
    "ax_nuke": 25 / 57.5, "ax_flip": 50 / 66.25,
}


def p_of(key: str, lam: float) -> float:
    b, d = P[key]
    return float(np.clip(b + lam * (d - b), 0.01, 0.99))


def p_bracket(target_pts: float, stop_pts: float, parent: str, lam: float) -> float:
    """Parent's lam-adjusted probability, shifted by the barrier difference when
    the bracket differs from the parent's (tightened stop / shrunk target)."""
    base = stop_pts / (target_pts + stop_pts)
    return float(np.clip(p_of(parent, lam) + (base - BARRIER[parent]), 0.01, 0.99))


# ---------------------------------------------------------------- Topstep ----

def ts_eval(rng: np.random.Generator, lam: float) -> tuple[bool, int]:
    """Returns (passed, days). Balance 50k, floor 48k ratcheting to lock at 50k,
    $1,000 stop, $1,500 day target with final-day shrink, pass at +3k & >=2 days."""
    bal, floor = 50_000.0, 48_000.0
    days = 0
    while True:
        days += 1
        room = bal - floor
        stop = min(1_000.0, room)
        remaining = 3_000.0 - (bal - 50_000.0)
        if 0 < remaining < 1_500.0:
            tgt_d = remaining
            p = p_bracket(tgt_d / 100.0, stop / 100.0, "ts_eval", lam)
        else:
            tgt_d = 1_500.0
            p = p_bracket(15.0, stop / 100.0, "ts_eval", lam)
        if rng.random() < p:
            bal += tgt_d
        else:
            bal -= stop
            if bal <= floor + 1e-9:
                return False, days
        if bal - 50_000.0 >= 3_000.0 and days >= 2:
            return True, days
        floor = min(50_000.0, max(floor, bal - 2_000.0))
        if days > 200:
            return False, days


def ts_funded(rng: np.random.Generator, lam: float) -> tuple[float, int]:
    """Express account: bal 0, floor -2k locking at 0. Lifecycle: nuke+4 flips ->
    5 flips -> renuke+4 flips -> 5 flips, retire at 4 payouts.
    Payout: bal >= 4k and >= 5 win days -> withdraw min(2k, bal/2)."""
    bal, floor = 0.0, -2_000.0
    payouts, withdrawn = 0, 0.0
    win_days = 0
    nuke_hit, nuke_tries = False, 0
    days = 0
    while payouts < 4 and days < 400:
        days += 1
        room = bal - floor
        nuke_cycle = payouts in (0, 2)
        if nuke_cycle and not nuke_hit:
            tgt_gross = 4_200.0 if nuke_tries >= 1 else 3_200.0
            tgt_pts = tgt_gross / 40.0            # 2 minis, $40/pt
            stop = min(1_000.0, room)
            parent = "ts_renuke" if nuke_tries >= 1 else "ts_nuke"
            p = p_bracket(tgt_pts, stop / 40.0, parent, lam)
            nuke_tries += 1
            if rng.random() < p:
                bal += tgt_gross - 50.0
                nuke_hit = True
                win_days += 1
            else:
                bal -= stop
                if bal <= floor + 1e-9:
                    return withdrawn, payouts
        else:
            stop = min(1_000.0, room)
            p = p_bracket(8.5, stop / 20.0, "ts_flip", lam)   # 1 mini, $20/pt
            if rng.random() < p:
                bal += 150.0
                win_days += 1
            else:
                bal -= stop
                if bal <= floor + 1e-9:
                    return withdrawn, payouts
        floor = min(0.0, max(floor, bal - 2_000.0))
        if bal + withdrawn >= 4_000.0 and win_days >= 5 and bal > 0:
            w = min(2_000.0, bal / 2.0)
            withdrawn += w
            bal -= w
            payouts += 1
            win_days = 0
            nuke_hit, nuke_tries = False, 0
    return withdrawn, payouts


def topstep_ticket(rng: np.random.Generator, lam: float) -> tuple[float, bool, float, int]:
    passed, _days = ts_eval(rng, lam)
    if not passed:
        return -85.0, False, 0.0, 0
    w, p = ts_funded(rng, lam)
    return w - 85.0, True, w, p


# ------------------------------------------------------------------- Apex ----

def ax_eval(rng: np.random.Generator, lam: float) -> tuple[bool, int]:
    """1-day $3,000 attempt (30/10); on a loss, 40/10 second attempt; then blown."""
    if rng.random() < p_of("ax_eval1", lam):
        return True, 1
    if rng.random() < p_of("ax_eval2", lam):
        return True, 2
    return False, 2


def ax_funded(rng: np.random.Generator, lam: float, horizon_days: int = 251) -> tuple[float, int]:
    """PA: bal 0, floor -2k locking at 0. Nuke (plain re-nuke on a miss) then
    $325 flips; payout $1,500 when bal-in-window >= 2,600, >= 5 qualifying days
    (net >= $250), best day <= 50% of window profit."""
    bal, floor = 0.0, -2_000.0
    withdrawn, payouts = 0.0, 0
    win_start = 0.0      # balance at window start
    qual_days = 0
    best_day = 0.0
    nuke_hit = False
    for _day in range(horizon_days):
        room = bal - floor
        stop = min(1_000.0, room)
        if not nuke_hit:
            p = p_bracket(32.5, stop / 40.0, "ax_nuke", lam)    # 2 minis
            if rng.random() < p:
                pnl = 1_250.0
                nuke_hit = True
            else:
                pnl = -stop
        else:
            p = p_bracket(16.25, stop / 20.0, "ax_flip", lam)   # 1 mini
            pnl = 305.0 if rng.random() < p else -stop
        bal += pnl
        if bal <= floor + 1e-9:
            return withdrawn, payouts
        if pnl >= 250.0:
            qual_days += 1
            best_day = max(best_day, pnl)
        floor = min(0.0, max(floor, bal - 2_000.0))
        wprofit = bal - win_start
        if (bal >= win_start + 2_600.0 and qual_days >= 5
                and best_day <= 0.5 * wprofit):
            withdrawn += 1_500.0
            bal -= 1_500.0
            payouts += 1
            win_start = bal
            qual_days = 0
            best_day = 0.0
            nuke_hit = False
    return withdrawn, payouts


def apex_ticket(rng: np.random.Generator, lam: float) -> tuple[float, bool, float, int]:
    passed, _days = ax_eval(rng, lam)
    if not passed:
        return -39.0, False, 0.0, 0
    w, p = ax_funded(rng, lam)
    return w - 39.0 - 139.0, True, w, p


# ------------------------------------------------------------------ sweep ----

def run(fn, lam: float, n: int, seed: int = 7):
    rng = np.random.default_rng(seed)
    ev = np.empty(n)
    passes = 0
    wsum = 0.0
    psum = 0
    for i in range(n):
        e, ok, w, p = fn(rng, lam)
        ev[i] = e
        passes += ok
        wsum += w
        psum += p
    return {
        "ev": ev.mean(), "ev_se": ev.std() / np.sqrt(n),
        "pass": passes / n,
        "w_per_funded": wsum / max(passes, 1),
        "payouts_per_funded": psum / max(passes, 1),
    }


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40_000

    print("=== validation at lam=1 (docs: TS pass 42.4%, $1,450/funded, EV +$530;"
          " AX pass 46.6%, ~$2,231/funded @1yr, EV ~ +$930) ===")
    for name, fn in [("Topstep", topstep_ticket), ("Apex", apex_ticket)]:
        r = run(fn, 1.0, n)
        print(f"{name:8s} pass {r['pass']:5.1%}  $/funded {r['w_per_funded']:7.0f}  "
              f"payouts/funded {r['payouts_per_funded']:.2f}  EV {r['ev']:+7.0f} +-{r['ev_se']:.0f}")

    print("\n=== edge sweep (EV per ticket, $) ===")
    lams = [-2.0, -1.5, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.25]
    print(f"{'lam':>6s} {'TS EV':>8s} {'TS pass':>8s} {'AX EV':>8s} {'AX pass':>8s} "
          f"{'evalWR':>7s} {'nukeWR':>7s} {'flipWR':>7s}")
    rows = []
    for lam in lams:
        rt = run(topstep_ticket, lam, n)
        ra = run(apex_ticket, lam, n)
        rows.append((lam, rt, ra))
        print(f"{lam:6.2f} {rt['ev']:+8.0f} {rt['pass']:8.1%} {ra['ev']:+8.0f} {ra['pass']:8.1%} "
              f"{p_of('ts_eval', lam):7.1%} {p_of('ts_nuke', lam):7.1%} {p_of('ts_flip', lam):7.1%}")

    import json
    with open("mc_sweep.json", "w") as f:
        json.dump([{"lam": l, "ts": t, "ax": a} for l, t, a in rows], f)
