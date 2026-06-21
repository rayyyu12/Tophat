"""Compare flip brackets: contract count vs point width (drive signal only)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from research.backtest_common import Bracket, collect_drive_entries, load_days, run_drive

BRACKETS = [
    ("2 mini live (3.75 tgt / 25 stp)", Bracket("a", 3.75, 25.0)),
    ("1 mini $-match (7.5 tgt / 50 stp)", Bracket("b", 7.5, 50.0)),
    ("1 mini yours (8.5 tgt / 50 stp)", Bracket("c", 8.5, 50.0)),
    ("1 mini tight (7.5 tgt / 25 stp)", Bracket("d", 7.5, 25.0)),
]


def main() -> None:
    days = load_days()
    entries = collect_drive_entries(days)
    mid = len(days) // 2
    is_e = collect_drive_entries(days[:mid])
    oos_e = collect_drive_entries(days[mid:])

    print(f"Days: {len(days)} | Drive entries: {len(entries)}/{len(days)}")
    print("Backtest resolves in POINTS from entry — contracts do not change win rate.")
    print("Dollar-equivalent brackets can use different point widths.\n")
    hdr = f"{'Bracket':<36} {'base':>6} {'drive':>6} {'edge':>6} {'IS':>6} {'OOS':>6} {'unres':>6}"
    print(hdr)
    print("-" * len(hdr))

    for label, br in BRACKETS:
        sg = run_drive(entries, br)
        sg_is = run_drive(is_e, br)
        sg_oos = run_drive(oos_e, br)
        n = sg["win"] + sg["loss"]
        wr = sg["win"] / n if n else 0.0
        is_n = sg_is["win"] + sg_is["loss"]
        oos_n = sg_oos["win"] + sg_oos["loss"]
        is_wr = sg_is["win"] / is_n if is_n else 0.0
        oos_wr = sg_oos["win"] / oos_n if oos_n else 0.0
        tot = n + sg["unresolved"]
        unres = sg["unresolved"] / tot if tot else 0.0
        print(
            f"{label:<36} {br.baseline:>5.1%} {wr:>5.1%} {wr - br.baseline:>+5.1%} "
            f"{is_wr:>5.1%} {oos_wr:>5.1%} {unres:>5.1%}"
        )


if __name__ == "__main__":
    main()
