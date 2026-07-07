# Reconstruction & stress analysis (2026-07-04)

> **2026-07-06 update (real tick data restored):** `sim_ticks_rth_1s.parquet`
> is now built from the operator's real NQ tick text exports (`data/NQ *.txt`)
> and validated bar-for-bar against the original research cache
> (`data/cache/nq_rth_1s.parquet`): all 5.51M overlapping 1s bars align, OHLC
> identical on 99.9998% (13 single-bar within-second tie diffs). Coverage
> 2025-06-23 .. 2026-06-30 (285 days) — it recovers 4 roll-Friday gaps the old
> expiry-based roll dropped and adds 9 days past 06-17. NOTE: the txt exports
> are **UTC**, not machine-local CT — `export_sim_ticks.py`'s --txt path was
> fixed accordingly (the .ncd path stays CT). The minute-bar day outcomes below
> keep their documented ~2-4pp tie bias and are superseded for forward use by
> the Simulation page running on the tick-derived cache.

The original `research/` scripts and `data/cache/nq_rth_1s.parquet` no longer
exist on this machine. This folder rebuilds the backtest independently from the
NinjaTrader 8 minute `.ncd` database (`Documents/NinjaTrader 8/db/minute`,
decoded via the reverse-engineered format from jrstokka/NinjaTraderNCDFiles)
and re-derives the lifecycle economics with a sweepable edge parameter.

## Pipeline

| script | what it does | output |
|---|---|---|
| `ncd_decode.py` | decodes NT8 minute .ncd files (timestamps are machine-local CT) | - |
| `rebuild_backtest.py` | front-month roll, drive entries at 09:45 ET, bar-by-bar bracket resolution for all Topstep + Apex brackets | `day_outcomes.csv` |
| `regime_analysis.py` | monthly / regime-block win rates, worst rolling stretches | `day_outcomes_enriched.csv`, `regime_wrs.json` |
| `mc_lifecycle.py` | corrected intraday-MLL lifecycle MC (Topstep 50K + Apex native), edge multiplier lambda sweep | `mc_sweep.json` |
| `regime_ev.py` | EV/ticket if each observed regime persisted (raw + minute-bias-corrected) | `regime_ev.json` |
| `fleet_sim.py` | full-fleet year simulation over REAL day sequences with same-day outcome correlation; scenarios: real year / worst-regime-tiled / coin-flip / launch-month grid | `fleet_results.json` |
| `make_charts.py` | 3-panel summary chart | `tophat_reality_check.png` |

## Known deltas vs the official numbers (docs/PROBABILITY.md)

- Minute bars vs 1s cache: ties (target+stop inside one bar) make this
  reconstruction ~2-4pp pessimistic on the tight brackets (eval, apex legs);
  nuke brackets have no ties. Full-period WRs land within ~2pp of docs after
  the optimistic tie bound.
- Coverage 2025-06-23 .. 2026-05-06 (224 days). The NT minute db stops at
  2026-05-06, so the last ~6 weeks of the original 251-day window are missing.
- MC validates at lam=1: TS pass 42.7% (docs 42.4), EV +$585 (docs +$530);
  Apex pass 46.0% (46.6), EV +$915 (+$930).

## Headline results

- Coin flip (edge fully faded): TS +$222/ticket, Apex +$163/ticket. Fleet
  median over a coin year: TS ~+$29k, AX ~+$40k.
- Break-even: lambda ~ -1.1 (TS) / -1.0 (AX) - the drive signal must be
  persistently ANTI-predictive (eval ~33%, nuke ~19.5% for TS) before a
  ticket is EV-negative. Max bleed even at lambda=-2 is ~ -$45/ticket.
- Regimes: Jun-Aug25 strong; Sep-Oct25 nukes below coin (16.3%); Nov-Feb ~ok;
  Mar-May26 weak everywhere (TS eval 29.8% vs 40% coin; below per-leg
  break-even). Worst-regime-persisted EV: TS +$77-155/ticket, AX +$136-435.
- Worst-regime YEAR (Mar-May26 tiled): TS median -$925 (60% negative years,
  p10 trough -$7.3k); Apex stays +$24.7k (its small nukes kept baseline WR).
