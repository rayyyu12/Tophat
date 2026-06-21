"""
One-off inspector to confirm the NinjaTrader tick-file format before we build
the real loader. Reads a small sample (not the whole 30M-line file) and prints
field structure, bid/last/ask ordering, timestamp decoding, time-of-day spread
(to infer timezone), and the file's first/last timestamps.

Usage:
    python inspect_data.py "data/NQ 03-26.Last.txt"
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime


def parse_line(line: str):
    # "20251211 060001 6640000;25771;25769.75;25771;1"
    parts = line.strip().split(";")
    if len(parts) != 5:
        return None
    dt_field, c1, c2, c3, vol = parts
    d, t, frac = dt_field.split()
    ts = datetime.strptime(d + t, "%Y%m%d%H%M%S")
    micros = int(frac) / 10_000_000.0  # .NET 100ns ticks -> seconds
    return ts, micros, float(c1), float(c2), float(c3), int(vol)


def last_line(path: str, blocksize: int = 4096) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        buf = b""
        pos = end
        while pos > 0:
            step = min(blocksize, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            if buf.count(b"\n") >= 2:
                break
        return buf.splitlines()[-1].decode("utf-8", "replace")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/NQ 03-26.Last.txt"
    size = os.path.getsize(path)
    print(f"file: {path}")
    print(f"size: {size/1e9:.2f} GB")

    n = 0
    bad_fields = 0
    order_violations = 0      # expect col2 (bid) <= col1 (last) <= col3 (ask)
    hours = Counter()
    first_ts = last_ts = None
    pmin, pmax = 1e18, -1e18
    sample = 300_000

    with open(path, "r") as f:
        for line in f:
            if n >= sample:
                break
            rec = parse_line(line)
            if rec is None:
                bad_fields += 1
                n += 1
                continue
            ts, _, last, bid, ask, vol = rec
            if not (bid <= last <= ask):
                order_violations += 1
            hours[ts.hour] += 1
            pmin, pmax = min(pmin, last), max(pmax, last)
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            n += 1

    print(f"\nparsed {n:,} sample lines | bad-field lines: {bad_fields}")
    print(f"bid<=last<=ask holds: {n - order_violations:,}/{n:,} "
          f"({100*(n-order_violations)/max(n,1):.2f}%)")
    print(f"sample price range (last): {pmin} .. {pmax}")
    print(f"sample first ts: {first_ts}  last ts: {last_ts}")
    print(f"\nfile first line : {open(path).readline().strip()}")
    print(f"file last  line : {last_line(path)}")

    print("\ntick count by hour (of the sample, local file tz):")
    for h in sorted(hours):
        print(f"  {h:02d}:00  {hours[h]:,}")


if __name__ == "__main__":
    main()
