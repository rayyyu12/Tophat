"""Editable strategy + automation settings (JSON), the dashboard Settings page model.

Holds the locked-but-tunable knobs in one place and builds the engine's
AccountConfig from them. See docs/STRATEGY.md for the chosen values.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from tophat.engine import AccountConfig
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
    eval_stop_pts: float = 9.5
    eval_min_days: int = 2

    funded_contracts: int = 2          # nukes
    flip_contracts: int = 1            # flips (1 mini = half commission, same prob)
    nuke_target_dollars: float = 3_200.0
    flip_target_dollars: float = 170.0
    payout_cap: float = 2_000.0
    winning_days_required: int = 5
    payouts_target: int = 4

    # --- automation ---
    nuke_entry_time: str = "09:45"     # ET; drive locks here
    flip_stagger_times: list[str] = field(
        default_factory=lambda: ["09:45", "10:00", "10:15", "10:30", "10:45"])
    max_nukes_per_day: int = 1         # decorrelation guarantee
    max_evals_per_day: int = 2         # copy ≤2 evals/day (STRATEGY §1, correlated-exposure cap)
    auto_execute: bool = False         # False = dry-run plans only (safe default)
    auto_disable_on_payout_ready: bool = True
    hedge_guard: bool = True           # skip an entry if the account isn't flat

    # --- display ---
    show_disabled: bool = True

    def to_account_config(self) -> AccountConfig:
        valid = {f.name for f in fields(AccountConfig)}
        return AccountConfig(**{k: v for k, v in asdict(self).items() if k in valid})


def load_settings(path: Path = SETTINGS_FILE) -> TopHatSettings:
    if not path.exists():
        return TopHatSettings()
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(TopHatSettings)}
    return TopHatSettings(**{k: v for k, v in raw.items() if k in known})


def save_settings(s: TopHatSettings, path: Path = SETTINGS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")


def update_settings(patch: dict, path: Path = SETTINGS_FILE) -> TopHatSettings:
    """Apply a partial update from the dashboard, validate, persist, return the result."""
    s = load_settings(path)
    known = {f.name for f in fields(TopHatSettings)}
    for k, v in patch.items():
        if k in known:
            setattr(s, k, v)
    save_settings(s, path)
    return s
