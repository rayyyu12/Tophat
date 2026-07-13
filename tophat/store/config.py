"""Editable strategy + automation settings (JSON), the dashboard Settings page model.

Holds the locked-but-tunable knobs in one place and builds the engine's
AccountConfig from them. See docs/STRATEGY.md for the chosen values.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from tophat.engine import AccountConfig
from tophat.store import tenant
from tophat.store.atomic import atomic_write_text
from tophat.store.paths import SETTINGS_FILE


@dataclass
class TopHatSettings:
    # --- account / strategy (mirrors AccountConfig editable fields) ---
    initial_balance: float = 50_000.0
    dll: float = 1_000.0
    trailing_drawdown: float = 2_000.0
    point_value: float = 20.0

    eval_contracts: int = 5
    eval_target_dollars: float = 3_000.0
    eval_target_pts: float = 15.5
    eval_stop_pts: float = 10.5         # $1,050: DLL + ~$50 slippage cushion so a red day books >= the full $1,000
    eval_min_days: int = 2

    funded_contracts: int = 2          # nukes
    flip_contracts: int = 1            # flips (1 mini = half commission, same prob)
    nuke_target_dollars: float = 3_200.0
    flip_target_dollars: float = 170.0
    payout_cap: float = 2_000.0
    winning_days_required: int = 5
    payouts_target: int = 4
    win_day_min: float = 150.0         # Topstep qualifying winning-day bar (NET $)

    # --- automation ---
    nuke_entry_time: str = "09:45"     # ET; drive locks here
    flip_stagger_times: list[str] = field(
        default_factory=lambda: ["09:45", "10:00", "10:15", "10:30", "10:45"])
    max_nukes_per_day: int = 1         # decorrelation guarantee
    max_evals_per_day: int = 2         # copy ≤2 evals/day (STRATEGY §1, correlated-exposure cap)
    eval_pipeline_depth_first: bool = True  # slots go to most-advanced evals first (front-load passes)
    entry_grace_min: int = 10          # fire at entry_time..+grace only; later = off-strategy, skip day
    # Nightly Auto-OCO Brackets probe (ET; 22:00 = 21:00 CT, evening session).
    # Empty string disables. Runs Sun-Thu nights; alerts via Discord webhook.
    oco_probe_time: str = "22:00"
    auto_execute: bool = False         # False = dry-run plans only (safe default)
    auto_disable_on_payout_ready: bool = True
    hedge_guard: bool = True           # skip an entry if the account isn't flat
    # Master switch for the Tradecopia bridge: False = every Rabbit pull gets
    # a clean 409 ("disabled in Settings") and no box ever touches the copier,
    # even while paired and running. The exporter separately refuses when no
    # active mirrors exist, so a Topstep-only stretch is quiet by itself.
    copier_sync_enabled: bool = True

    # --- notifications ---
    # Per-user Discord webhook (Settings page). Consumed by server-side
    # posts (copier apply results). Empty = notifications off for this user.
    discord_webhook_url: str = ""

    # --- display ---
    show_disabled: bool = True

    def to_account_config(self) -> AccountConfig:
        valid = {f.name for f in fields(AccountConfig)}
        return AccountConfig(**{k: v for k, v in asdict(self).items() if k in valid})


def load_settings(path: Path | None = None) -> TopHatSettings:
    path = tenant.resolve(SETTINGS_FILE) if path is None else path
    if not path.exists():
        return TopHatSettings()
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(TopHatSettings)}
    return TopHatSettings(**{k: v for k, v in raw.items() if k in known})


def save_settings(s: TopHatSettings, path: Path | None = None) -> None:
    path = tenant.resolve(SETTINGS_FILE) if path is None else path
    atomic_write_text(path, json.dumps(asdict(s), indent=2))


def _norm_hhmm(t: str) -> str:
    """Normalize '9:45' -> '09:45'. Raises ValueError on junk — every scheduler
    comparison is a zero-padded string compare, so a malformed time would
    silently never fire (or fire always)."""
    parts = str(t).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time {t!r} (expected HH:MM)")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid time {t!r} (expected HH:MM)")
    return f"{h:02d}:{m:02d}"


def update_settings(patch: dict, path: Path | None = None) -> TopHatSettings:
    """Apply a partial update from the dashboard, validate, persist, return the result."""
    path = tenant.resolve(SETTINGS_FILE) if path is None else path
    s = load_settings(path)
    known = {f.name for f in fields(TopHatSettings)}
    for k, v in patch.items():
        if k in known:
            setattr(s, k, v)
    s.nuke_entry_time = _norm_hhmm(s.nuke_entry_time)
    s.flip_stagger_times = [_norm_hhmm(t) for t in s.flip_stagger_times]
    s.oco_probe_time = _norm_hhmm(s.oco_probe_time) if str(s.oco_probe_time).strip() else ""
    u = str(s.discord_webhook_url or "").strip()
    if u and not (u.startswith("https://") and "discord" in u.split("/")[2]
                  and "/api/webhooks/" in u):
        raise ValueError("discord_webhook_url must be a Discord webhook URL "
                         "(https://discord.com/api/webhooks/...)")
    s.discord_webhook_url = u
    save_settings(s, path)
    return s
