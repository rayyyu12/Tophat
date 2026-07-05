"""Decode NinjaTrader 8 minute .ncd files into a pandas frame.

Format per jrstokka/NinjaTraderNCDFiles (MIT). Header: uint32 tickSizeTime,
float64 tickSizePrice, float64 fileStartPrice, uint64 fileStartDateTicks
(.NET 100ns ticks). Records: 2 control bytes then variable-length deltas.
Open delta is relative to the PREVIOUS BAR'S OPEN; high/low relative to this
bar's open; close relative to this bar's low. Multi-byte ints are BIG-endian.
"""
from __future__ import annotations

import glob
import os
import struct
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

DB_MINUTE = r"C:\Users\rtxft\Documents\NinjaTrader 8\db\minute"

# .NET ticks epoch: 0001-01-01. Offset to Unix epoch in 100ns ticks.
NET_TICKS_AT_UNIX_EPOCH = 621355968000000000


def _read_be(buf: bytes, pos: int, n: int) -> tuple[int, int]:
    return int.from_bytes(buf[pos:pos + n], "big"), pos + n


def decode_minute_file(path: str) -> list[tuple[datetime, float, float, float, float, int]]:
    with open(path, "rb") as f:
        buf = f.read()
    tick_size_time, tick, start_price, start_ticks = struct.unpack_from("<Id d Q".replace(" ", ""), buf, 0)
    pos = 28
    last_dt_ticks = start_ticks
    last_open = start_price
    out = []
    n = len(buf)
    while pos < n:
        m1 = buf[pos]; m2 = buf[pos + 1]; pos += 2

        # time (minutes delta) -- mask1 bits 0-1; check 3 (Time4) first, then 1, then 2
        tbits = m1 & 3
        if tbits == 3:
            td, pos = _read_be(buf, pos, 4)
        elif tbits == 1:
            td, pos = _read_be(buf, pos, 1)
        elif tbits == 2:
            td, pos = _read_be(buf, pos, 2)
        else:
            td = 1
        last_dt_ticks += td * 600_000_000  # ticks per minute

        # open -- mask1 bits 2-3 (Open4=12 first, Open1=4, Open2=8), signed delta
        obits = m1 & 12
        if obits == 12:
            od, pos = _read_be(buf, pos, 4)
            od -= 0x40000000
        elif obits == 4:
            od, pos = _read_be(buf, pos, 1)
            od -= 128
        elif obits == 8:
            od, pos = _read_be(buf, pos, 2)
            od -= 32768
        else:
            od = 0
        o = last_open + od * tick

        # high -- mask2 bits 4-5: High4=48 first, High1=16, High2=32
        hbits = m2 & 48
        if hbits == 48:
            hd, pos = _read_be(buf, pos, 4)
        elif hbits == 16:
            hd, pos = _read_be(buf, pos, 1)
        elif hbits == 32:
            hd, pos = _read_be(buf, pos, 2)
        else:
            hd = 0
        h = o + hd * tick

        # low -- mask2 bits 6-7: Low4=192 first, Low1=64, Low2=128
        lbits = m2 & 192
        if lbits == 192:
            ld, pos = _read_be(buf, pos, 4)
        elif lbits == 64:
            ld, pos = _read_be(buf, pos, 1)
        elif lbits == 128:
            ld, pos = _read_be(buf, pos, 2)
        else:
            ld = 0
        lo = o - ld * tick

        # close -- mask2 bits 0-1: Close4=3 first, Close1=1, Close2=2 (rel. to LOW)
        cbits = m2 & 3
        if cbits == 3:
            cd, pos = _read_be(buf, pos, 4)
        elif cbits == 1:
            cd, pos = _read_be(buf, pos, 1)
        elif cbits == 2:
            cd, pos = _read_be(buf, pos, 2)
        else:
            cd = 0
        c = lo + cd * tick

        # volume -- mask1 top bits, check order: 240,192,160,128,96,32,64
        vm = 1
        if (m1 & 240) == 240:
            vd, pos = _read_be(buf, pos, 8)
        elif (m1 & 192) == 192:
            vd, pos = _read_be(buf, pos, 4)
        elif (m1 & 160) == 160:
            vd, pos = _read_be(buf, pos, 2)
        elif (m1 & 128) == 128:
            vd, pos = _read_be(buf, pos, 1); vm = 1000
        elif (m1 & 96) == 96:
            vd, pos = _read_be(buf, pos, 1); vm = 500
        elif (m1 & 32) == 32:
            vd, pos = _read_be(buf, pos, 1)
        elif (m1 & 64) == 64:
            vd, pos = _read_be(buf, pos, 1); vm = 100
        else:
            raise ValueError(f"bad volume flag m1={m1:08b} at {path}:{pos}")
        v = vd * vm

        unix_us = (last_dt_ticks - NET_TICKS_AT_UNIX_EPOCH) // 10
        out.append((unix_us, o, h, lo, c, v))
        last_open = o
    return out


def contract_expiry(code: str) -> date:
    mm, yy = code.split()[1].split("-")
    month, year = int(mm), 2000 + int(yy)
    d = date(year, month, 1)
    fridays = [d.replace(day=x) for x in range(1, 29)
               if d.replace(day=x).weekday() == 4]
    return fridays[2]


def load_contract(code: str) -> pd.DataFrame:
    rows = []
    for path in sorted(glob.glob(os.path.join(DB_MINUTE, code, "*.Last.ncd"))):
        try:
            rows.extend(decode_minute_file(path))
        except Exception as e:  # noqa: BLE001
            print(f"  WARN {code} {os.path.basename(path)}: {e}")
    df = pd.DataFrame(rows, columns=["us", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["us"], unit="us")
    df = df.drop(columns=["us"]).set_index("dt").sort_index()
    df["contract"] = code
    return df


if __name__ == "__main__":
    code = "NQ 06-26"
    files = sorted(glob.glob(os.path.join(DB_MINUTE, code, "*.Last.ncd")))
    print(f"{code}: {len(files)} files, {os.path.basename(files[0])} .. {os.path.basename(files[-1])}")
    recs = decode_minute_file(files[-1])
    df = pd.DataFrame(recs, columns=["us", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["us"], unit="us")
    print(df.head(3).to_string())
    print(df.tail(3).to_string())
    print("bars:", len(df), "| price range:", df.low.min(), "-", df.high.max())
    bad = ((df.high < df[["open", "close"]].max(axis=1)) | (df.low > df[["open", "close"]].min(axis=1))).sum()
    print("OHLC violations:", bad)
    # sanity: volume-by-hour peak should be 13:30-16:00 UTC if timestamps are UTC
    print(df.groupby(df.dt.dt.hour)["volume"].sum().sort_values(ascending=False).head(5))
