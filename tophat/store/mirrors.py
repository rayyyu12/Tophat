"""Mirror (follower) accounts: API-less prop-firm accounts modeled by inference.

A mirror is an account at a follower firm (Lucid / Tradeify / Apex) that the
external copy-trader trades and TopHat never touches. TopHat *models* it: when the
mirror's leader reconciles a closed trade, the same scaled outcome is booked here
(services/mirror_sync.py) and the firm's own accounting rules advance. All balances
are INFERRED until the operator syncs them against the firm's dashboard.

Bookkeeping is profit-relative (equity = $ above the account's starting balance),
which unifies eval ($50k start) and funded ($0 start) under one trailing-floor
formula: floor = min(0, peak - trailing), locking at breakeven once earned.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from tophat.store.atomic import atomic_write_text
from tophat.store.firms import FOLLOWER_FIRMS, get_firm
from tophat.store.paths import MIRRORS_FILE

# Mirror lifecycle phases. `waiting` = eval passed & funded account issued, but
# holding flat for a fresh leader to pair with (Tradeify wait-for-fresh policy).
PHASES = ("eval", "funded", "waiting", "passed", "blown", "retired")
TERMINAL_PHASES = ("blown", "retired")

# Apex channel tags (informational grouping for the copier plan; the actual signal
# source is leader_id like any other mirror).
CHANNELS = ("", "nuke", "flip", "eval")


@dataclass
class MirrorAccount:
    mirror_id: str
    firm: str                      # FirmProfile.key (follower firms only)
    account_number: str = ""       # the firm's real account id (operator-entered)
    alias: str = ""
    leader_id: int | None = None   # Topstep account_id this mirror copies (None = unmapped)
    multiplier: float = 1.0        # copier contract scale vs the leader
    channel: str = ""              # apex: nuke | flip | eval
    phase: str = "eval"
    enabled: bool = True
    # --- inferred accounting (profit-relative dollars) ---
    equity: float = 0.0
    peak: float = 0.0              # peak EOD profit (trailing-floor anchor)
    days_traded: int = 0
    win_days: int = 0              # qualifying days this window (net >= firm.win_day_min)
    window_profit: float = 0.0     # net profit since last payout (apex consistency window)
    best_day: float = 0.0          # best single win day this window
    eval_best_day: float = 0.0     # best win day during eval (consistency rules)
    payouts_taken: int = 0
    payout_ready: bool = False
    last_outcome_date: str = ""    # last propagated trading day
    last_day_pnl: float = 0.0      # last booked day P&L (nuke-landed detection, UI)
    last_nuke_date: str = ""       # last day this mirror held the apex nuke slot
    last_verified: str = ""        # last manual balance sync (YYYY-MM-DD)
    notes: str = ""
    created_at: float = 0.0

    def floor(self) -> float:
        return min(0.0, self.peak - get_firm(self.firm).trailing)

    def room(self) -> float:
        return self.equity - self.floor()

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES


def _to_dict(m: MirrorAccount) -> dict:
    return asdict(m)


def _from_dict(d: dict) -> MirrorAccount:
    known = {f.name for f in fields(MirrorAccount)}
    kw = {k: v for k, v in d.items() if k in known}
    return MirrorAccount(**kw)


_IO_LOCK = threading.Lock()


def load_mirrors(path: Path = MIRRORS_FILE) -> dict[str, MirrorAccount]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: _from_dict(v) for k, v in raw.get("mirrors", {}).items()}


def _write(mirrors: dict[str, MirrorAccount], path: Path) -> None:
    payload = {"mirrors": {k: _to_dict(m) for k, m in mirrors.items()}}
    atomic_write_text(path, json.dumps(payload, indent=2))


def save_mirrors(mirrors: dict[str, MirrorAccount], path: Path = MIRRORS_FILE) -> None:
    with _IO_LOCK:
        _write(mirrors, path)


def merge_save_mirrors(mirrors: dict[str, MirrorAccount], ids,
                       path: Path = MIRRORS_FILE) -> None:
    """Persist ONLY `ids`, merged over on-disk contents (same contract as
    states.merge_save — concurrent writers can't clobber each other's entries)."""
    with _IO_LOCK:
        disk = load_mirrors(path)
        for i in ids:
            if i in mirrors:
                disk[i] = mirrors[i]
        _write(disk, path)


def delete_mirror(mirror_id: str, path: Path = MIRRORS_FILE) -> bool:
    with _IO_LOCK:
        disk = load_mirrors(path)
        if mirror_id not in disk:
            return False
        del disk[mirror_id]
        _write(disk, path)
        return True


def next_mirror_id(firm_key: str, mirrors: dict[str, MirrorAccount]) -> str:
    """Auto-slug: 'lucid-01', 'tradeify-03', 'apex-12' — stable, human-readable."""
    prefix = get_firm(firm_key).label.lower()
    taken = {m.mirror_id for m in mirrors.values()}
    n = 1
    while f"{prefix}-{n:02d}" in taken:
        n += 1
    return f"{prefix}-{n:02d}"


def create_mirror(firm_key: str, *, account_number: str = "", alias: str = "",
                  leader_id: int | None = None, multiplier: float | None = None,
                  phase: str = "eval", path: Path = MIRRORS_FILE) -> MirrorAccount:
    """Register a follower account. Defaults to a fresh eval at the firm's standard
    copier scale. Raises ValueError on unknown firms / bad phase."""
    if firm_key not in FOLLOWER_FIRMS:
        raise ValueError(f"unknown follower firm {firm_key!r} "
                         f"(known: {sorted(FOLLOWER_FIRMS)})")
    if phase not in PHASES:
        raise ValueError(f"invalid phase {phase!r}")
    firm = get_firm(firm_key)
    with _IO_LOCK:
        disk = load_mirrors(path)
        mid = next_mirror_id(firm_key, disk)
        scale = firm.copier_scale_eval if phase == "eval" else 1.0
        m = MirrorAccount(
            mirror_id=mid, firm=firm_key, account_number=str(account_number).strip(),
            alias=alias.strip(), leader_id=leader_id,
            multiplier=float(multiplier) if multiplier is not None else scale,
            phase=phase, created_at=time.time(),
        )
        disk[mid] = m
        _write(disk, path)
        return m


def with_mirror(mirror_id: str, fn, path: Path = MIRRORS_FILE) -> MirrorAccount | None:
    """Load -> apply `fn(mirror)` -> persist, all under the store lock. Returns the
    updated mirror, or None if unknown. `fn` may raise ValueError for bad states."""
    with _IO_LOCK:
        disk = load_mirrors(path)
        m = disk.get(mirror_id)
        if m is None:
            return None
        fn(m)
        _write(disk, path)
        return m


_PATCHABLE = {
    "account_number", "alias", "leader_id", "multiplier", "channel", "phase",
    "enabled", "equity", "peak", "days_traded", "win_days", "window_profit",
    "best_day", "eval_best_day", "payouts_taken", "payout_ready", "notes",
}


def patch_mirror(mirror_id: str, patch: dict, *, today: str = "",
                 path: Path = MIRRORS_FILE) -> MirrorAccount | None:
    """Apply a partial operator edit. `sync_balance` in the patch sets equity to the
    firm-dashboard profit figure and stamps last_verified=today."""
    with _IO_LOCK:
        disk = load_mirrors(path)
        m = disk.get(mirror_id)
        if m is None:
            return None
        for k, v in patch.items():
            if k not in _PATCHABLE:
                continue
            if k == "phase":
                if v not in PHASES:
                    raise ValueError(f"invalid phase {v!r}")
                m.phase = str(v)
            elif k == "channel":
                if v not in CHANNELS:
                    raise ValueError(f"invalid channel {v!r}")
                m.channel = str(v)
            elif k == "leader_id":
                m.leader_id = int(v) if v is not None else None
            elif k in ("enabled", "payout_ready"):
                setattr(m, k, bool(v))
            elif k in ("days_traded", "win_days", "payouts_taken"):
                setattr(m, k, int(v))
            elif k in ("account_number", "alias", "notes"):
                setattr(m, k, str(v).strip())
            else:
                setattr(m, k, float(v))
        if patch.get("sync_balance") is not None:
            m.equity = float(patch["sync_balance"])
            m.peak = max(m.peak, m.equity)
            m.last_verified = today
        _write(disk, path)
        return m
