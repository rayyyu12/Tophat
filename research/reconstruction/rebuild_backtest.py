"""Rebuild the drive backtest from NinjaTrader minute .ncd files.

Coverage: 2025-06-23 .. 2026-05-06 (NT db stops there; original cache ran to
2026-06-17). Timestamps in .ncd are machine-local Central Time; CT+1h = ET
(same DST rules). Methodology mirrors research/backtest.py from the first
commit: drive direction = sign(close(09:44) - open(09:30)); fill at the 09:45
bar open; resolve bar-by-bar to 16:00 ET; unresolved excluded from WR;
minute-bar tie (target & stop in same bar) reported both ways (pess=stop wins,
opt=target wins).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ncd_decode import load_contract

CONTRACTS = ["NQ 09-25", "NQ 12-25", "NQ 03-26", "NQ 06-26"]
START, END = "2025-06-23", "2026-05-06"

BRACKETS = {
    # name: (target_pts, stop_pts)
    "ts_eval_15_10":    (15.0, 10.0),
    "ts_eval_155_95":   (15.5, 9.5),    # legacy, for docs comparison
    "ts_nuke_80_25":    (80.0, 25.0),
    "ts_renuke_105_25": (105.0, 25.0),
    "ts_flip_85_50":    (8.5, 50.0),
    "ax_eval_30_10":    (30.0, 10.0),
    "ax_eval2_40_10":   (40.0, 10.0),
    "ax_nuke_325_25":   (32.5, 25.0),
    "ax_flip_1625_50":  (16.25, 50.0),
}


def build_series() -> pd.DataFrame:
    frames = []
    for c in CONTRACTS:
        print("decoding", c, "...")
        frames.append(load_contract(c))
    df = pd.concat(frames)
    # CT -> ET is always +1h (identical DST rules)
    df.index = df.index + pd.Timedelta(hours=1)
    df = df.sort_index()
    df["date"] = df.index.date
    # RTH only
    df = df[(df.index.time >= pd.Timestamp("09:30").time())
            & (df.index.time <= pd.Timestamp("16:00").time())]
    # roll: per date keep the contract with max RTH volume
    vol = df.groupby(["date", "contract"])["volume"].sum().reset_index()
    pick = vol.loc[vol.groupby("date")["volume"].idxmax()].set_index("date")["contract"]
    df = df[df["contract"] == df["date"].map(pick)]
    df = df[(df["date"] >= pd.Timestamp(START).date())
            & (df["date"] <= pd.Timestamp(END).date())]
    return df


def resolve(hi: np.ndarray, lo: np.ndarray, entry: float, direction: int,
            tgt_pts: float, stp_pts: float) -> str:
    """Return win/loss/tie-first/unresolved. tie = both barriers in one bar."""
    if direction == 1:
        t, s = entry + tgt_pts, entry - stp_pts
        hit_t, hit_s = hi >= t, lo <= s
    else:
        t, s = entry - tgt_pts, entry + stp_pts
        hit_t, hit_s = lo <= t, hi >= s
    it = np.argmax(hit_t) if hit_t.any() else -1
    is_ = np.argmax(hit_s) if hit_s.any() else -1
    if it == -1 and is_ == -1:
        return "unresolved"
    if it == -1:
        return "loss"
    if is_ == -1:
        return "win"
    if it < is_:
        return "win"
    if is_ < it:
        return "loss"
    return "tie"


def main() -> None:
    df = build_series()
    days = []
    rows = []
    for d, g in df.groupby("date", sort=True):
        g = g.sort_index()
        orng = g.between_time("09:30", "09:44")
        post = g.between_time("09:45", "16:00")
        if len(orng) < 10 or len(post) < 60:
            continue
        or_open = float(orng["open"].iloc[0])
        or_close = float(orng["close"].iloc[-1])
        if or_close == or_open:
            direction = 0
        else:
            direction = 1 if or_close > or_open else -1
        entry = float(post["open"].iloc[0])
        hi = post["high"].to_numpy()
        lo = post["low"].to_numpy()
        row = {"date": d, "dir": direction, "entry": entry,
               "or_pts": abs(or_close - or_open),
               "day_range": float(g["high"].max() - g["low"].min())}
        if direction != 0:
            for name, (t, s) in BRACKETS.items():
                row[name] = resolve(hi, lo, entry, direction, t, s)
        rows.append(row)
        days.append(d)
    out = pd.DataFrame(rows).set_index("date")
    out.to_csv("day_outcomes.csv")
    print(f"\n{len(out)} trading days {out.index[0]} .. {out.index[-1]}, "
          f"{(out['dir'] != 0).sum()} with a drive direction")

    traded = out[out["dir"] != 0]
    print("\n=== Full-period drive win rates (pess = tie->stop / opt = tie->win) ===")
    print(f"{'bracket':18s} {'pess':>6s} {'opt':>6s} {'base':>6s} {'n':>4s} {'unres':>6s} {'ties':>5s}")
    for name, (t, s) in BRACKETS.items():
        r = traded[name]
        res = r[r != "unresolved"]
        ties = (res == "tie").sum()
        wins = (res == "win").sum()
        pess = wins / len(res)
        opt = (wins + ties) / len(res)
        base = s / (t + s)
        print(f"{name:18s} {pess:6.1%} {opt:6.1%} {base:6.1%} {len(res):4d} "
              f"{(r == 'unresolved').sum():6d} {ties:5d}")


if __name__ == "__main__":
    main()
