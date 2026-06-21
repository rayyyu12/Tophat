"""Account registry: enable/disable accounts, aliases, and UI settings (JSON file)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from tophat.store.paths import REGISTRY_FILE


@dataclass
class AccountEntry:
    enabled: bool = True
    alias: str = ""
    notes: str = ""


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
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "settings": {
            "confirm_nukes": reg.settings.confirm_nukes,
            "show_disabled": reg.settings.show_disabled,
        },
        "accounts": {
            str(k): {"enabled": e.enabled, "alias": e.alias, "notes": e.notes}
            for k, e in reg.accounts.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
