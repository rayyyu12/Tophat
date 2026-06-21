"""Load NQ 1s bars for arbitrary ET entry windows (RTH or Globex)."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from research.data_loader import (
    CACHE_DIR,
    COLS,
    DATA_DIR,
    MARKET_TZ,
    contract_from_path,
    roll_front_month,
)

GLOBEX_CACHE = os.path.join(CACHE_DIR, "nq_globex_1s.parquet")
GLOBEX_UTC_HOURS = {f"{h:02d}" for h in list(range(0, 11)) + list(range(22, 24))}


@dataclass
class DayData:
    date: object
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    secs: np.ndarray
    entry_idx: np.ndarray


def _load_globex_file(path: str, chunksize: int = 5_000_000) -> pd.DataFrame:
    """Load evening + overnight ticks (18:00-04:00 ET) with a wide UTC prefilter."""
    frames = []
    for chunk in pd.read_csv(path, sep=";", header=None, names=COLS,
                             dtype={"dt": str, "last": "float32", "bid": "float32",
                                    "ask": "float32", "volume": "int32"},
                             chunksize=chunksize):
        hours = chunk["dt"].str.slice(9, 11)
        chunk = chunk[hours.isin(GLOBEX_UTC_HOURS)]
        if chunk.empty:
            continue
        ts = pd.to_datetime(chunk["dt"].str.slice(0, 15),
                            format="%Y%m%d %H%M%S", utc=False)
        ts = ts.dt.tz_localize("UTC").dt.tz_convert(MARKET_TZ)
        df = pd.DataFrame({"last": chunk["last"].to_numpy(),
                           "volume": chunk["volume"].to_numpy()},
                          index=pd.DatetimeIndex(ts))
        df = df.between_time("18:00", "04:00")
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


def _build_globex_cache(force: bool = False) -> pd.DataFrame:
    if not force and os.path.isfile(GLOBEX_CACHE):
        return pd.read_parquet(GLOBEX_CACHE)

    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.txt")))
    if not files:
        raise FileNotFoundError(f"No tick files in {DATA_DIR}/")

    parts = []
    for f in files:
        df = _load_globex_file(f)
        if df.empty:
            continue
        parts.append(df)

    allbars = pd.concat(parts).sort_index()
    rolled = roll_front_month(allbars, 7)
    rolled["date"] = rolled.index.date
    os.makedirs(CACHE_DIR, exist_ok=True)
    rolled.to_parquet(GLOBEX_CACHE)
    return rolled


def _overnight_chunk(all_df: pd.DataFrame, session_date: date, start: str, end: str) -> pd.DataFrame | None:
    """Bars from `start` on session_date through `end` (may cross midnight)."""
    d0 = session_date
    d1 = session_date + timedelta(days=1)
    t0 = pd.Timestamp(f"{d0} {start}").tz_localize(MARKET_TZ)
    t1 = pd.Timestamp(f"{d0} {end}").tz_localize(MARKET_TZ)
    if t1 <= t0:
        t1 = pd.Timestamp(f"{d1} {end}").tz_localize(MARKET_TZ)
    chunk = all_df.loc[t0:t1]
    return chunk if len(chunk) > 0 else None


def load_rth_days(
    entry_start: str,
    entry_end: str,
    cache: str = "data/cache/nq_rth_1s.parquet",
) -> list[DayData]:
    df = pd.read_parquet(cache)
    days: list[DayData] = []
    for d, g in df.groupby("date", sort=True):
        g = g.sort_index()
        win = g.index.indexer_between_time(entry_start, entry_end)
        if len(win) == 0:
            continue
        op = g["open"].to_numpy(dtype="float64")
        days.append(
            DayData(
                date=d,
                open=op,
                high=g["high"].to_numpy(dtype="float64"),
                low=g["low"].to_numpy(dtype="float64"),
                close=g["close"].to_numpy(dtype="float64"),
                secs=(g.index.asi8 // 1_000_000_000).astype("int64"),
                entry_idx=np.asarray(win, dtype="int64"),
            )
        )
    return days


def load_globex_days(
    entry_start: str,
    entry_end: str,
    resolve_end: str = "03:00",
) -> list[DayData]:
    """
    One trade per calendar date that has an entry window.

    Session chunk: entry_start .. resolve_end (crosses midnight when resolve_end
    is earlier than entry_start on the clock).
    """
    df = _build_globex_cache()
    session_dates = sorted({ts.date() for ts in df.index if ts.hour >= 18})
    days: list[DayData] = []
    for d in session_dates:
        chunk = _overnight_chunk(df, d, entry_start, resolve_end)
        if chunk is None or chunk.empty:
            continue
        win = chunk.index.indexer_between_time(entry_start, entry_end)
        if len(win) == 0:
            continue
        op = chunk["open"].to_numpy(dtype="float64")
        days.append(
            DayData(
                date=d,
                open=op,
                high=chunk["high"].to_numpy(dtype="float64"),
                low=chunk["low"].to_numpy(dtype="float64"),
                close=chunk["close"].to_numpy(dtype="float64"),
                secs=(chunk.index.asi8 // 1_000_000_000).astype("int64"),
                entry_idx=np.asarray(win, dtype="int64"),
            )
        )
    return days


def load_evening_reopen_days(entry_start: str = "18:15", entry_end: str = "18:30") -> list[DayData]:
    """5:15-5:30 PM CT = 6:15-6:30 PM ET; resolve through 21:00 ET same day."""
    df = _build_globex_cache()
    session_dates = sorted({ts.date() for ts in df.index if ts.hour >= 18})
    days: list[DayData] = []
    for d in session_dates:
        t0 = pd.Timestamp(f"{d} {entry_start}").tz_localize(MARKET_TZ)
        t1 = pd.Timestamp(f"{d} 21:00").tz_localize(MARKET_TZ)
        chunk = df.loc[t0:t1]
        if chunk.empty:
            continue
        win = chunk.index.indexer_between_time(entry_start, entry_end)
        if len(win) == 0:
            continue
        op = chunk["open"].to_numpy(dtype="float64")
        days.append(
            DayData(
                date=d,
                open=op,
                high=chunk["high"].to_numpy(dtype="float64"),
                low=chunk["low"].to_numpy(dtype="float64"),
                close=chunk["close"].to_numpy(dtype="float64"),
                secs=(chunk.index.asi8 // 1_000_000_000).astype("int64"),
                entry_idx=np.asarray(win, dtype="int64"),
            )
        )
    return days
