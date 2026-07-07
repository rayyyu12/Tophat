"""Build the Simulation page's bar cache from NinjaTrader TICK data.

Aggregates NQ ticks to 1-second OHLC bars (the original research cache
resolution, data/cache/nq_rth_1s.parquet), RTH only, front-month rolled by
daily volume. Output: sim_ticks_rth_1s.parquet next to this script - the app
(tophat/store/simdata.py) loads only that file and never touches NinjaTrader.

Two input paths:

1. NT8 tick db (default):  Documents/NinjaTrader 8/db/tick/<NQ contract>/
   The .ncd tick decoder follows the jrstokka/NinjaTraderNCDFiles layout but
   has NOT been verified against real tick files yet (none on this machine).
   Decoded output is validated HARD (monotonic timestamps, prices near the
   file start price, RTH volume peak); the script refuses to write the cache
   if validation fails - use path 2 in that case.

2. NT8 text export (--txt DIR):  Tools > Historical Data > Export (Text),
   tick granularity. One file per contract named like "NQ 06-26.Last.txt",
   lines "yyyyMMdd HHmmss[ fffffff];last[;bid;ask];volume".
   TIMEZONE: text exports are UTC (verified 2026-07-06 against the original
   research cache data/cache/nq_rth_1s.parquet and research/data_loader.py:
   export-range boundaries land at midnight CT = 05:00/06:00 UTC across DST,
   and the RTH volume peak sits at 13:30-16:00 UTC). The .ncd db, by
   contrast, stores machine-local CT. Each path converts to ET itself.

Usage:
    python research/reconstruction/export_sim_ticks.py
    python research/reconstruction/export_sim_ticks.py --txt path/to/exports
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import struct
from pathlib import Path

import numpy as np
import pandas as pd

DB_TICK = r"C:\Users\rtxft\Documents\NinjaTrader 8\db\tick"
OUT = Path(__file__).resolve().parent / "sim_ticks_rth_1s.parquet"

NET_TICKS_AT_UNIX_EPOCH = 621355968000000000  # .NET ticks at 1970-01-01


def _read_be(buf: bytes, pos: int, n: int) -> tuple[int, int]:
    return int.from_bytes(buf[pos:pos + n], "big"), pos + n


def decode_tick_file(path: str) -> list[tuple[int, float, int]]:
    """Decode one NT8 tick .ncd -> [(unix_us, price, volume)].

    UNVERIFIED against real tick data: same container as the minute format
    (28-byte header), records assumed one mask byte + time delta (bits 0-1,
    scaled by header tickSizeTime in 100ns units) + signed price delta
    (bits 2-3) + volume (top nibble, minute-format flag order).
    validate_ticks() decides whether the result is trustworthy.
    """
    with open(path, "rb") as f:
        buf = f.read()
    tick_size_time, tick, start_price, start_ticks = struct.unpack_from(
        "<IddQ", buf, 0)
    time_scale = tick_size_time or 1
    pos = 28
    dt_ticks = start_ticks
    price = start_price
    out: list[tuple[int, float, int]] = []
    n = len(buf)
    while pos < n:
        m = buf[pos]; pos += 1

        tbits = m & 3
        if tbits == 3:
            td, pos = _read_be(buf, pos, 4)
        elif tbits == 1:
            td, pos = _read_be(buf, pos, 1)
        elif tbits == 2:
            td, pos = _read_be(buf, pos, 2)
        else:
            td = 0
        dt_ticks += td * time_scale

        pbits = m & 12
        if pbits == 12:
            pd_, pos = _read_be(buf, pos, 4)
            pd_ -= 0x40000000
        elif pbits == 4:
            pd_, pos = _read_be(buf, pos, 1)
            pd_ -= 128
        elif pbits == 8:
            pd_, pos = _read_be(buf, pos, 2)
            pd_ -= 32768
        else:
            pd_ = 0
        price += pd_ * tick

        vm = 1
        if (m & 240) == 240:
            vd, pos = _read_be(buf, pos, 8)
        elif (m & 192) == 192:
            vd, pos = _read_be(buf, pos, 4)
        elif (m & 160) == 160:
            vd, pos = _read_be(buf, pos, 2)
        elif (m & 128) == 128:
            vd, pos = _read_be(buf, pos, 1); vm = 1000
        elif (m & 96) == 96:
            vd, pos = _read_be(buf, pos, 1); vm = 500
        elif (m & 32) == 32:
            vd, pos = _read_be(buf, pos, 1)
        elif (m & 64) == 64:
            vd, pos = _read_be(buf, pos, 1); vm = 100
        else:
            raise ValueError(f"bad volume flag {m:08b} at {path}:{pos}")

        out.append(((dt_ticks - NET_TICKS_AT_UNIX_EPOCH) // 10, price, vd * vm))
    return out


def validate_ticks(df: pd.DataFrame, source: str, *,
                   peak_hours: range = range(8, 16)) -> None:
    """Hard sanity gate for decoded ticks - refuse to build a wrong cache.

    `peak_hours` is where the RTH volume burst must land in the SOURCE
    timezone: 08:30-15:00 for machine-local-CT .ncd data (default),
    13:30-20:00 for UTC text exports. Passing the wrong range is what let the
    original +1h-CT assumption slip through on UTC data - the 13:30 UTC open
    burst overlaps the 8..15 CT window.
    """
    problems = []
    if len(df) < 1000:
        problems.append(f"only {len(df)} ticks")
    if not df["ts"].is_monotonic_increasing:
        problems.append("timestamps not monotonic")
    med = float(df["price"].median())
    lo, hi = float(df["price"].min()), float(df["price"].max())
    if med <= 0 or lo < 0.5 * med or hi > 2.0 * med:
        problems.append(f"price range implausible ({lo} .. {hi}, median {med})")
    by_hour = df.groupby(df["ts"].dt.hour)["volume"].sum()
    if not by_hour.empty and by_hour.idxmax() not in peak_hours:
        problems.append(f"volume peaks at hour {by_hour.idxmax()} "
                        f"(expected {peak_hours.start}..{peak_hours.stop - 1} "
                        "in the source tz) - timestamps look wrong")
    if problems:
        raise SystemExit(
            f"TICK DECODE VALIDATION FAILED for {source}:\n  - "
            + "\n  - ".join(problems)
            + "\nThe .ncd tick decoder needs verification against this data. "
              "Export the ticks as text from NinjaTrader (Tools > Historical "
              "Data > Export) and re-run with --txt <dir>.")


def load_ncd_contract(code: str) -> pd.DataFrame:
    rows: list[tuple[int, float, int]] = []
    for path in sorted(glob.glob(os.path.join(DB_TICK, code, "*.Last.ncd"))):
        rows.extend(decode_tick_file(path))
    df = pd.DataFrame(rows, columns=["us", "price", "volume"])
    df["ts"] = pd.to_datetime(df["us"], unit="us")
    df = df.drop(columns=["us"]).sort_values("ts", kind="stable").reset_index(drop=True)
    validate_ticks(df, f"{code} (.ncd)")   # .ncd db is machine-local CT
    # CT -> ET is a constant +1h (identical DST rules)
    df["ts"] = df["ts"] + pd.Timedelta(hours=1)
    df["contract"] = code
    return df


_TXT_LINE = re.compile(r"^(\d{8}) (\d{6})(?: (\d+))?;([0-9.]+)(?:;[0-9.]*)*;(\d+)$")


def load_txt_contract(path: str) -> pd.DataFrame:
    """Parse one NT8 tick text export: yyyyMMdd HHmmss[ frac];last[;bid;ask];vol.

    Text exports are UTC (see module docstring); validated in UTC, then
    converted DST-aware to ET so downstream never sees the source tz.
    """
    ts, px, vol = [], [], []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            m = _TXT_LINE.match(ln.strip())
            if not m:
                continue
            ts.append(f"{m.group(1)} {m.group(2)}")
            px.append(float(m.group(4)))
            vol.append(int(m.group(5)))
    df = pd.DataFrame({
        "ts": pd.to_datetime(ts, format="%Y%m%d %H%M%S"),
        "price": px, "volume": vol}).sort_values("ts", kind="stable").reset_index(drop=True)
    code = Path(path).name.split(".")[0]
    validate_ticks(df, f"{code} (txt)", peak_hours=range(13, 20))  # UTC source
    df["ts"] = (df["ts"].dt.tz_localize("UTC")
                .dt.tz_convert("America/New_York").dt.tz_localize(None))
    df["contract"] = code
    return df


def ticks_to_1s(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate ticks to 1-second OHLC bars (per contract)."""
    df = df.set_index("ts")
    o = df.groupby([pd.Grouper(freq="1s"), "contract"]).agg(
        open=("price", "first"), high=("price", "max"),
        low=("price", "min"), close=("price", "last"),
        volume=("volume", "sum")).dropna().reset_index()
    return o


def build_cache(frames: list[pd.DataFrame]) -> pd.DataFrame:
    # loaders hand over ET wall-clock timestamps (each converts its own source tz)
    bars = pd.concat([ticks_to_1s(f) for f in frames], ignore_index=True)
    bars["date"] = bars["ts"].dt.strftime("%Y-%m-%d")
    bars["time"] = bars["ts"].dt.strftime("%H:%M:%S")
    bars = bars[(bars["time"] >= "09:30:00") & (bars["time"] <= "16:00:00")]
    # front-month roll: per date keep the contract with the most RTH volume
    vol = bars.groupby(["date", "contract"])["volume"].sum().reset_index()
    pick = vol.loc[vol.groupby("date")["volume"].idxmax()] \
        .set_index("date")["contract"]
    bars = bars[bars["contract"] == bars["date"].map(pick)]
    return bars.sort_values(["date", "time"], kind="stable")[
        ["date", "time", "open", "high", "low", "close"]].reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--txt", help="directory of NT8 tick text exports "
                                  "(NQ*.Last.txt) instead of the .ncd db")
    args = ap.parse_args()

    frames: list[pd.DataFrame] = []
    if args.txt:
        files = sorted(glob.glob(os.path.join(args.txt, "NQ*.txt")))
        if not files:
            raise SystemExit(f"no NQ*.txt files in {args.txt}")
        for p in files:
            print("parsing", Path(p).name, "...")
            frames.append(load_txt_contract(p))
    else:
        codes = sorted(Path(p).name for p in glob.glob(os.path.join(DB_TICK, "NQ *")))
        if not codes:
            raise SystemExit(
                f"no NQ tick contracts under {DB_TICK}.\nImport tick data into "
                "NinjaTrader first, or use --txt with text exports.")
        for c in codes:
            print("decoding", c, "...")
            frames.append(load_ncd_contract(c))

    out = build_cache(frames)
    out.to_parquet(OUT, index=False)
    days = out["date"].nunique()
    print(f"wrote {OUT.name}: {len(out)} 1s bars, {days} days, "
          f"{out['date'].min()} .. {out['date'].max()}")


if __name__ == "__main__":
    main()
