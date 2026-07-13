"""Persist per-account engine lifecycle state (JSON file, not a database)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from tophat.engine import AccountState, Phase
from tophat.store import tenant
from tophat.store.atomic import atomic_write_text
from tophat.store.paths import STATES_FILE


def state_to_dict(s: AccountState) -> dict:
    """Public serializer for API responses."""
    return _state_to_dict(s)


def _state_to_dict(s: AccountState) -> dict:
    return {
        "phase": s.phase.value,
        "equity": s.equity,
        "peak_equity_eod": s.peak_equity_eod,
        "base_balance": s.base_balance,
        "days_traded": s.days_traded,
        "payouts_taken": s.payouts_taken,
        "winning_days_this_cycle": s.winning_days_this_cycle,
        "nuke_tries_this_cycle": s.nuke_tries_this_cycle,
        "nuke_hit_this_cycle": s.nuke_hit_this_cycle,
        "locked_out_today": s.locked_out_today,
        "payout_ready": s.payout_ready,
        "last_fire_date": s.last_fire_date,
        "last_seen_trading_day": s.last_seen_trading_day,
        "last_seen_balance": s.last_seen_balance,
        "pending_label": s.pending_label,
        "pending_entry_balance": s.pending_entry_balance,
        "pending_target_dollars": s.pending_target_dollars,
        "pending_stop_dollars": s.pending_stop_dollars,
        "pending_date": s.pending_date,
    }


def _dict_to_state(d: dict) -> AccountState:
    return AccountState(
        phase=Phase(d.get("phase", "eval")),
        equity=float(d.get("equity", 50_000)),
        peak_equity_eod=float(d.get("peak_equity_eod", 50_000)),
        base_balance=float(d.get("base_balance", 50_000)),
        days_traded=int(d.get("days_traded", 0)),
        payouts_taken=int(d.get("payouts_taken", 0)),
        winning_days_this_cycle=int(d.get("winning_days_this_cycle", 0)),
        nuke_tries_this_cycle=int(d.get("nuke_tries_this_cycle", 0)),
        nuke_hit_this_cycle=bool(d.get("nuke_hit_this_cycle", False)),
        locked_out_today=bool(d.get("locked_out_today", False)),
        payout_ready=bool(d.get("payout_ready", False)),
        last_fire_date=str(d.get("last_fire_date", "")),
        last_seen_trading_day=str(d.get("last_seen_trading_day", "")),
        last_seen_balance=float(d.get("last_seen_balance", 0.0)),
        pending_label=str(d.get("pending_label", "")),
        pending_entry_balance=float(d.get("pending_entry_balance", 0.0)),
        pending_target_dollars=float(d.get("pending_target_dollars", 0.0)),
        pending_stop_dollars=float(d.get("pending_stop_dollars", 0.0)),
        pending_date=str(d.get("pending_date", "")),
    )


# Serializes writers within this process. Concurrent load→mutate→save cycles
# (automation thread, WS snapshot builds, API threadpool handlers) would otherwise
# clobber each other's entries — e.g. a snapshot save losing a just-recorded fire.
_IO_LOCK = threading.Lock()


def load_all(path: Path | None = None) -> dict[int, AccountState]:
    path = tenant.resolve(STATES_FILE) if path is None else path
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {int(k): _dict_to_state(v) for k, v in raw.items()}


def _write(states: dict[int, AccountState], path: Path) -> None:
    payload = {str(k): _state_to_dict(v) for k, v in states.items()}
    atomic_write_text(path, json.dumps(payload, indent=2))


def save_all(states: dict[int, AccountState], path: Path | None = None) -> None:
    path = tenant.resolve(STATES_FILE) if path is None else path
    with _IO_LOCK:
        _write(states, path)


def merge_save(states: dict[int, AccountState], ids,
               path: Path | None = None) -> None:
    """Persist ONLY `ids`, merged over the current on-disk contents.

    Use this from any writer that changed a subset of accounts, so it can't
    overwrite entries another writer updated since this writer's load()."""
    path = tenant.resolve(STATES_FILE) if path is None else path
    with _IO_LOCK:
        disk = load_all(path)
        for i in ids:
            if i in states:
                disk[i] = states[i]
        _write(disk, path)


def get_or_create(states: dict[int, AccountState], account_id: int) -> AccountState:
    if account_id not in states:
        states[account_id] = AccountState()
    return states[account_id]
