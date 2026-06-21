"""
Load the NinjaTrader NQ tick exports in data/ into a clean, fast cache.

Pipeline per contract file:
  1. Stream-read in chunks; pre-filter to RTH-ish UTC hours (cheap).
  2. Parse timestamps, convert SOURCE_TZ -> America/New_York (DST-aware).
  3. Keep the configured ET session window.
  4. Resample to 1-second OHLCV bars (only seconds that traded).
Across files: roll to a front-month series (dedupe overlapping dates), then
write one Parquet file to data/cache/.

IMPORTANT - TIMEZONE: the source files are assumed to be UTC (inferred from the
22:00 maintenance-halt gap and the 14:00-16:00 UTC volume peak = 09:00-11:00 ET).
If that's wrong, change --source-tz; everything downstream keys off the ET
conversion here.

File format (confirmed): "YYYYMMDD HHMMSS fffffff;Last;Bid;Ask;Volume"
  (fffffff = .NET 100ns fractional seconds; bid <= last <= ask).

Usage:
    python data_loader.py
    python data_loader.py --source-tz UTC --rth-start 09:30 --rth-end 16:00
"""

from __future__ import annotations

import argparse
import glob
import os
from datetime import date, timedelta

import pandas as pd

DATA_DIR = "data"
CACHE_DIR = os.path.join("data", "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "nq_rth_1s.parquet")

MARKET_TZ = "America/New_York"
# Pre-filter: ET 09:30-16:00 maps to UTC 13:30-21:00 across DST -> hours 13..21.
# Only valid when source tz is UTC; widen if you change --source-tz.
PREFILTER_UTC_HOURS = {f"{h:02d}" for h in range(13, 22)}

COLS = ["dt", "last", "bid", "ask", "volume"]


def contract_expiry(code: str) -> date:
    """'NQ 03-26' -> 3rd Friday of Mar 2026 (CME equity index expiry)."""
    mm, yy = code.split()[1].split("-")
    month, year = int(mm), 2000 + int(yy)
    d = date(year, month, 1)
    fridays = [d.replace(day=x) for x in range(1, 22)
               if d.replace(day=x).weekday() == 4]
    return fridays[2]


def contract_from_path(path: str) -> str:
    return os.path.basename(path).split(".")[0]   # "NQ 03-26"


def load_one(path: str, source_tz: str, win_start: str, win_end: str,
             chunksize: int = 5_000_000) -> pd.DataFrame:
    frames = []
    for chunk in pd.read_csv(path, sep=";", header=None, names=COLS,
                             dtype={"dt": str, "last": "float32", "bid": "float32",
                                    "ask": "float32", "volume": "int32"},
                             chunksize=chunksize):
        hours = chunk["dt"].str.slice(9, 11)
        chunk = chunk[hours.isin(PREFILTER_UTC_HOURS)]
        if chunk.empty:
            continue
        ts = pd.to_datetime(chunk["dt"].str.slice(0, 15),
                            format="%Y%m%d %H%M%S", utc=False)
        ts = ts.dt.tz_localize(source_tz).dt.tz_convert(MARKET_TZ)
        # Keep a tz-AWARE index (pd.DatetimeIndex preserves tz; .values would
        # strip it back to naive UTC and break the ET window filter).
        df = pd.DataFrame({"last": chunk["last"].to_numpy(),
                           "volume": chunk["volume"].to_numpy()},
                          index=pd.DatetimeIndex(ts))
        df = df.between_time(win_start, win_end)
        if df.empty:
            continue
        bars = df.resample("1s").agg(open=("last", "first"),
                                     high=("last", "max"),
                                     low=("last", "min"),
                                     close=("last", "last"),
                                     volume=("volume", "sum")).dropna(subset=["close"])
        frames.append(bars)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames)
    out["contract"] = contract_from_path(path)
    return out


def roll_front_month(df: pd.DataFrame, buffer_days: int) -> pd.DataFrame:
    """Keep only the front-month contract for each calendar date."""
    expiries = {c: contract_expiry(c) for c in df["contract"].unique()}
    ordered = sorted(expiries, key=lambda c: expiries[c])

    def front_for(d: date) -> str:
        for c in ordered:
            if expiries[c] - timedelta(days=buffer_days) >= d:
                return c
        return ordered[-1]

    df = df.copy()
    df["date"] = df.index.date
    front_by_date = {d: front_for(d) for d in df["date"].unique()}
    df["front"] = df["date"].map(front_by_date)
    return df[df["contract"] == df["front"]].drop(columns=["front"])


def main() -> None:
    p = argparse.ArgumentParser(description="Build the NQ RTH 1s cache")
    p.add_argument("--source-tz", default="UTC")
    p.add_argument("--rth-start", default="09:30")
    p.add_argument("--rth-end", default="16:00")
    p.add_argument("--roll-buffer", type=int, default=7,
                   help="days before expiry to roll to the next contract")
    args = p.parse_args()

    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.txt")))
    if not files:
        print(f"No .txt files found in {DATA_DIR}/")
        return

    parts = []
    for f in files:
        print(f"loading {f} ...", flush=True)
        df = load_one(f, args.source_tz, args.rth_start, args.rth_end)
        if not df.empty:
            print(f"  -> {len(df):,} 1s bars, "
                  f"{df.index.min()} .. {df.index.max()}")
            parts.append(df)

    allbars = pd.concat(parts).sort_index()
    rolled = roll_front_month(allbars, args.roll_buffer)

    os.makedirs(CACHE_DIR, exist_ok=True)
    rolled.to_parquet(CACHE_FILE)

    n_days = rolled["date"].nunique()
    print(f"\nCACHE WRITTEN: {CACHE_FILE}")
    print(f"  rows (1s bars): {len(rolled):,}")
    print(f"  trading days  : {n_days}")
    print(f"  date range    : {rolled['date'].min()} .. {rolled['date'].max()}")
    print(f"  contracts used: {sorted(rolled['contract'].unique())}")
    print(f"  avg bars/day  : {len(rolled)//max(n_days,1):,}")


if __name__ == "__main__":
    main()
