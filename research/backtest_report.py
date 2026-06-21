"""
NinjaTrader-style performance report for TopHat drive backtests (FLIP + NUKE).

Uses the same 1-second RTH cache and drive signal as backtest_flip.py /
backtest_nuke.py, then prints Strategy Analyzer–style statistics.

Usage:
    python research/backtest_report.py
    python research/backtest_report.py --capital 50000 --commission 5
    python research/backtest_report.py --flip-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from research.backtest_common import Bracket, CACHE_FILE, collect_drive_entries, load_days
from research.backtest_flip import FLIP_LIVE, FLIP_LEGACY
from research.backtest_nuke import NUKE_DLL, NUKE_NODLL
from research.backtest_stats import (
    StrategyConfig,
    analyze_trades,
    build_trades,
    format_nt_report,
)

FLIP_CFG = StrategyConfig("TopHat Drive Flip", FLIP_LIVE, contracts=1)
NUKE_CFG = StrategyConfig("TopHat Drive Nuke (DLL)", NUKE_DLL, contracts=2)


def main() -> None:
    ap = argparse.ArgumentParser(description="NT-style stats for flip + nuke drive backtests")
    ap.add_argument("--capital", type=float, default=50_000.0,
                    help="starting capital (default 50k Topstep account)")
    ap.add_argument("--commission", type=float, default=0.0,
                    help="commission per round-turn trade")
    ap.add_argument("--slip", type=float, default=0.0,
                    help="adverse entry slippage in points")
    ap.add_argument("--cache", default=CACHE_FILE)
    ap.add_argument("--flip-only", action="store_true")
    ap.add_argument("--nuke-only", action="store_true")
    ap.add_argument("--legacy-flip", action="store_true",
                    help="flip bracket 7.5/50 instead of 8.5/50")
    ap.add_argument("--no-dll", action="store_true",
                    help="nuke bracket 100/50 instead of DLL 100/25")
    args = ap.parse_args()

    days = load_days(args.cache)
    entries = collect_drive_entries(days)

    flip_cfg = FLIP_CFG
    if args.legacy_flip:
        flip_cfg = StrategyConfig("TopHat Drive Flip (legacy 7.5/50)",
                                  FLIP_LEGACY, contracts=1)

    nuke_br = NUKE_NODLL if args.no_dll else NUKE_DLL
    nuke_label = "TopHat Drive Nuke (no DLL 2:1)" if args.no_dll else NUKE_CFG.name
    nuke_cfg = StrategyConfig(nuke_label, nuke_br, contracts=2)

    configs: list[StrategyConfig] = []
    if not args.nuke_only:
        configs.append(flip_cfg)
    if not args.flip_only:
        configs.append(nuke_cfg)

    for cfg in configs:
        trades = build_trades(entries, cfg, slip=args.slip)
        report = analyze_trades(
            trades, cfg,
            initial_capital=args.capital,
            commission_per_trade=args.commission,
            slip=args.slip,
        )
        print(format_nt_report(
            report,
            initial_capital=args.capital,
            commission_per_trade=args.commission,
            slip=args.slip,
        ))
        print()


if __name__ == "__main__":
    main()
