# research/archive

Exploratory dead-ends, kept for provenance but **not part of the locked strategy**
(RTH 09:45 drive momentum — see `docs/STRATEGY.md`).

- `session_loader.py` — loader for Globex/overnight (evening reopen, Asia) sessions.
- `backtest_flip_sessions.py` — flip win rates across overnight/alternate session windows.

These tested whether an overnight or alternate-session entry beat the RTH open drive.
It did not, so they are archived. Run from the project root: `python -m research.archive.backtest_flip_sessions`.
The Globex cache they use (`data/cache/nq_globex_1s.parquet`) is regenerable and git-ignored.
