"""Append-only log of live trading events (data/trade_log.jsonl).

One JSON object per line. Written when a trade reconciles (win/loss/flat), when
a leader payout is marked withdrawn, and when a mirror payout is marked paid -
the raw feed the Analytics page aggregates. Append-only by design: history is
never rewritten, a crash can at worst lose the final line, and aggregation
stays a pure read.

Event shapes (fields beyond these are allowed and ignored by readers):
  trade:  {ts, date, type, owner, account_id, label, outcome, pnl, balance, phase}
  payout: {ts, date, type, account_id | mirror_id, amount, estimated, source}
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tophat.store import tenant
from tophat.store.paths import TRADE_LOG_FILE

_ET = ZoneInfo("America/New_York")
# Serializes appends within this process (automation thread + API handlers).
_LOCK = threading.Lock()


def log_event(event_type: str, **fields) -> dict:
    """Append one event. Timestamps are ET, matching the trading schedule."""
    now = datetime.now(_ET)
    evt = {"ts": now.isoformat(timespec="seconds"),
           "date": now.strftime("%Y-%m-%d"), "type": event_type, **fields}
    line = json.dumps(evt, separators=(",", ":"), default=str)
    log_file = tenant.resolve(TRADE_LOG_FILE)
    with _LOCK:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return evt


def read_events(path: Path | None = None) -> list[dict]:
    """All events, oldest first. Tolerates a torn final line after a crash."""
    path = tenant.resolve(TRADE_LOG_FILE) if path is None else path
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out
