"""Append new NinjaTrader tick exports to the Simulation page's bar cache.

The "I downloaded another month of NQ data" routine:

1. NinjaTrader: Tools > Historical Data > Export — instrument (e.g. NQ 09-26),
   type Tick, format Text. One "NQ 09-26.Last.txt" per contract.
2. python research/reconstruction/import_ticks.py "path\\to\\NQ 09-26.Last.txt"
   (any mix of files and directories; directories are scanned for NQ*.txt)
3. git add + commit sim_ticks_rth_1s.parquet — the cache ships in the repo,
   which is how the new days reach every user and deployment.

Merge semantics: imported days REPLACE existing bars for the same dates (a
fuller re-export wins); every other existing day is kept. Within one import
batch the per-date front-month roll picks the contract with the most RTH
volume — the same rule as a full rebuild (export_sim_ticks.py). A running
server picks the change up on its next read: the bar cache and the coverage
sidecar are both keyed to the parquet's file stat, no restart needed.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

# repo root on the path so tophat.* imports work no matter where this is run from
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from export_sim_ticks import OUT, build_cache, load_txt_contract  # noqa: E402


def collect_files(paths: list[str]) -> list[str]:
    files: list[str] = []
    for p in paths:
        pth = Path(p)
        if pth.is_dir():
            hits = sorted(glob.glob(str(pth / "NQ*.txt")))
            if not hits:
                raise SystemExit(f"no NQ*.txt files in {pth}")
            files.extend(hits)
        elif pth.is_file():
            files.append(str(pth))
        else:
            raise SystemExit(f"not found: {pth}")
    return files


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+",
                    help="NT8 tick text exports (NQ*.Last.txt) and/or directories")
    ap.add_argument("--out", type=Path, default=OUT,
                    help="target parquet (default: the repo cache; override for testing)")
    args = ap.parse_args()
    out: Path = args.out

    frames = []
    for p in collect_files(args.paths):
        print("parsing", Path(p).name, "...")
        frames.append(load_txt_contract(p))   # hard-validates tz/monotonic/price
    new = build_cache(frames)
    if new.empty:
        raise SystemExit("exports contained no RTH bars - nothing to import")

    old = pd.read_parquet(out) if out.exists() else new.iloc[0:0]
    new_dates = set(new["date"])
    replaced = sorted(new_dates & set(old["date"]))
    merged = (pd.concat([old[~old["date"].isin(new_dates)], new],
                        ignore_index=True)
              .sort_values(["date", "time"], kind="stable")
              .reset_index(drop=True))

    # atomic replace - a running server must never see a torn parquet
    fd, tmp = tempfile.mkstemp(suffix=".parquet", dir=out.parent)
    os.close(fd)
    try:
        merged.to_parquet(tmp, index=False)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    from tophat.store import simdata
    simdata.reset_cache()
    cov = simdata.write_coverage_sidecar(out)
    print(f"imported {len(new_dates)} day(s) "
          f"({min(new_dates)} .. {max(new_dates)})"
          + (f", {len(replaced)} replaced existing bars" if replaced else ""))
    print(f"cache now: {cov['days']} days, {cov['date_from']} .. {cov['date_to']}")
    print(f"commit the updated {out.name} to ship it to all users")


if __name__ == "__main__":
    main()
