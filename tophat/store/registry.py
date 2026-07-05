"""Account registry: enable/disable accounts, aliases, and UI settings (JSON file)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from tophat.store.atomic import atomic_write_text
from tophat.store.paths import REGISTRY_FILE


@dataclass
class AccountEntry:
    enabled: bool = True
    alias: str = ""
    notes: str = ""
    force_inactive: bool = False  # operator override when broker canTrade is misleading
    exclude_analytics: bool = False  # keep out of Analytics counts/spend (pre-project accounts)
    enabled_at: float = 0.0       # epoch when last enabled; eval-slot tiebreaker (newest waits)
    signal_plan: str = ""         # non-empty = signal channel (engine.SIGNAL_PLAN_TEMPLATES
                                  # key): fires that bracket for the copier, bypasses the
                                  # strategy fleet entirely


@dataclass
class AppSettings:
    confirm_nukes: bool = True
    show_disabled: bool = True


@dataclass
class AccountRegistry:
    accounts: dict[int, AccountEntry] = field(default_factory=dict)
    settings: AppSettings = field(default_factory=AppSettings)

    def entry(self, account_id: int) -> AccountEntry:
        if account_id not in self.accounts:
            self.accounts[account_id] = AccountEntry()
        return self.accounts[account_id]

    def is_enabled(self, account_id: int) -> bool:
        return self.entry(account_id).enabled

    def toggle(self, account_id: int) -> bool:
        e = self.entry(account_id)
        e.enabled = not e.enabled
        if e.enabled:
            e.enabled_at = time.time()   # newly enabled -> waits behind already-active accounts
        return e.enabled


def load_registry(path: Path = REGISTRY_FILE) -> AccountRegistry:
    if not path.exists():
        return AccountRegistry()
    raw = json.loads(path.read_text(encoding="utf-8"))
    reg = AccountRegistry()
    reg.settings = AppSettings(**raw.get("settings", {}))
    for k, v in raw.get("accounts", {}).items():
        reg.accounts[int(k)] = AccountEntry(**v)
    return reg


def save_registry(reg: AccountRegistry, path: Path = REGISTRY_FILE) -> None:
    payload = {
        "settings": {
            "confirm_nukes": reg.settings.confirm_nukes,
            "show_disabled": reg.settings.show_disabled,
        },
        "accounts": {
            str(k): {
                "enabled": e.enabled,
                "alias": e.alias,
                "notes": e.notes,
                "force_inactive": e.force_inactive,
                "exclude_analytics": e.exclude_analytics,
                "enabled_at": e.enabled_at,
                "signal_plan": e.signal_plan,
            }
            for k, e in reg.accounts.items()
        },
    }
    atomic_write_text(path, json.dumps(payload, indent=2))
