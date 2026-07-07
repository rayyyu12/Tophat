# Simulation Page - Plan & Status

Status: V1 IMPLEMENTED (2026-07-04). This doc now records the shipped design
and what was deliberately deferred.

## What shipped

Dashboard page (nav: under Analytics) that backtests and Monte-Carlos a
configurable strategy against the cached NQ minute bars, with saved templates.

Components:

| piece | file |
|---|---|
| bar cache export (NT8 TICK data -> 1s bars parquet) | `research/reconstruction/export_sim_ticks.py` -> `sim_ticks_rth_1s.parquet` |
| bar loader / per-entry-time day views | `tophat/store/simdata.py` |
| pure MC engine | `tophat/services/simulator.py` (`run_sim(params, views)`) |
| templates + stored runs | `tophat/store/simstore.py` -> `data/sim_templates.json`, `data/sim_runs/<id>.json` |
| API | `GET /api/sim/meta`, `POST /api/sim/run`, `GET/POST/DELETE /api/sim/templates[...]` |
| UI | Simulation section in `tophat/server/static/index.html` |

## Inputs (implemented)

- lifecycle: full (eval -> funded), eval only, funded only, single bracket daily
- direction rule: drive momentum (09:30 open -> last close before entry),
  always long/short, coin flip
- entry time (entered in local tz, stored/simulated as ET), optional date range
- account: starting balances, DLL, trailing MLL, point value
- eval leg: contracts, target/stop pts, profit target $, min days
- funded legs: nuke/flip contracts + $ targets, winning days per payout,
  payout cap, payouts-then-retire
- costs: ticket + activation (prefillable from a firm profile, editable)
- run: paths (<=20k, paths x days <= 5M), seed, bootstrap or sequential
  sampling, max days, tickets fleet multiplier
- minute-bar tie rule: pessimistic (stop wins, research default) or optimistic
  - run both to bracket the ~2-4pp tie bias on tight brackets

## Fidelity contract

- Day outcomes resolve first-touch on real minute bars; unresolved days exit at
  the 16:00 close and classify through the live half-way thresholds.
- The lifecycle advances through the LIVE code: `engine.decide()` picks each
  day's bracket; `services/lifecycle` classify/advance functions book it;
  payouts use `mark_payout_taken` + min(cap, equity/2). Sim cannot silently
  diverge from live behavior.
- Deterministic: same params + data + seed => identical result
  (tested in `tests/test_simulator.py`).

## Outputs (implemented)

Success probability (per-mode definition), net-cash distribution
(mean/median/p5/p95, banked vs costs), blow/pass/payout/retire probabilities,
days-to-outcome, net-cash fan chart (p10/p25/p50/p75/p90 over 130 days),
per-leg win rates vs coin floor and `analytics.MODEL_WR`, fleet-of-N-tickets
distribution (independent-tickets approximation).

Money model: net cash = payouts banked - tickets/activations. Account equity
above base is paper (the firm resets it) and never counts as cash.

## Data source: TICK data (cache BUILT 2026-07-06 from the real exports)

The simulator runs on 1-SECOND bars aggregated from NinjaTrader TICK data (the
original research fidelity - `data/cache/nq_rth_1s.parquet` was 1s). The cache
(`research/reconstruction/sim_ticks_rth_1s.parquet`) is now built from the
operator's real tick text exports (`data/NQ *.txt`) and validated bar-for-bar
against the original research cache: all 5.51M overlapping bars align, OHLC
identical on 99.9998%. Coverage 2025-06-23 .. 2026-06-30 (285 usable days).

To rebuild (e.g. after exporting newer ticks from NinjaTrader):

    python research/reconstruction/export_sim_ticks.py            # NT8 tick db
    python research/reconstruction/export_sim_ticks.py --txt data # NT text exports

TIMEZONE (bug fixed 2026-07-06): NT text exports are **UTC**; the .ncd db is
machine-local CT. The exporter converts each source correctly now — an earlier
build assumed CT for the txt path and produced shifted bars that slipped past
the volume-peak validation (the 13:30 UTC open burst overlaps the CT check
window; the validator now takes the expected peak range per source).

CAUTION: the .ncd tick decoder remains UNVERIFIED (no .ncd tick files existed
on the machine where it was written). It hard-validates its output and refuses
to write the cache on failure - fall back to text exports (--txt) in that
case. Restart the server (or reload the page in a new process) after building.

## Deferred (revisit when needed)

1. Fleet-mode scheduler fidelity: eval/nuke slot caps and correlated same-day
   outcomes across accounts (fleet_sim.py models this offline). V1 treats
   tickets as independent draws - optimistic on correlation.
2. Follower-firm accounting in-sim (Tradeify consistency, Apex payout gate);
   V1 payouts are Topstep half-profit-cap style. Firm presets prefill costs
   and basic rules only.
3. Fresh bar source beyond the NT db (ProjectX history endpoint would remove
   the manual NT export step).
4. Job polling for very long runs - current cap keeps runs seconds-scale in
   the FastAPI threadpool, so the POST simply returns the result.
