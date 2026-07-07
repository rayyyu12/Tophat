"""ProjectX API credentials, stored with the key encrypted at rest.

Each entry pairs a ProjectX username with exactly one API key (the username is
unique — at most one key per user, per the dashboard spec). Keys are encrypted
via `store.crypto` before they hit disk and are never returned to the client in
list responses — only masked. The raw key is exposed solely through the explicit
authenticated reveal endpoint. See docs/BUILD_PLAN.md §7.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from tophat.store import crypto, tenant
from tophat.store.atomic import atomic_write_text
from tophat.store.paths import CREDENTIALS_FILE


@dataclass
class Credential:
    username: str
    api_key: str            # plaintext in memory; encrypted on disk
    base_url: str = ""
    enabled: bool = True
    created_at: float = 0.0


def load_credentials(path: Path | None = None) -> list[Credential]:
    path = tenant.resolve(CREDENTIALS_FILE) if path is None else path
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[Credential] = []
    for item in raw.get("credentials", []):
        try:
            api_key = crypto.decrypt(item["api_key_enc"])
        except Exception:
            api_key = ""   # unreadable (e.g. machine secret rotated) — surface empty
        out.append(Credential(
            username=item.get("username", ""),
            api_key=api_key,
            base_url=item.get("base_url", ""),
            enabled=bool(item.get("enabled", True)),
            created_at=float(item.get("created_at", 0.0)),
        ))
    return out


def save_credentials(creds: list[Credential], path: Path | None = None) -> None:
    path = tenant.resolve(CREDENTIALS_FILE) if path is None else path
    payload = {"credentials": [
        {
            "username": c.username,
            "api_key_enc": crypto.encrypt(c.api_key),
            "base_url": c.base_url,
            "enabled": c.enabled,
            "created_at": c.created_at or time.time(),
        }
        for c in creds
    ]}
    atomic_write_text(path, json.dumps(payload, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def add_credential(username: str, api_key: str, base_url: str = "",
                   path: Path | None = None) -> list[Credential]:
    """Add (or replace) the key for a username. One key per username — upsert."""
    path = tenant.resolve(CREDENTIALS_FILE) if path is None else path
    username = (username or "").strip()
    api_key = (api_key or "").strip()
    if not username or not api_key:
        raise ValueError("username and api key are both required")
    creds = [c for c in load_credentials(path) if c.username.lower() != username.lower()]
    creds.append(Credential(username=username, api_key=api_key,
                            base_url=(base_url or "").strip(), created_at=time.time()))
    save_credentials(creds, path)
    return creds


def delete_credential(username: str, path: Path | None = None) -> list[Credential]:
    path = tenant.resolve(CREDENTIALS_FILE) if path is None else path
    creds = [c for c in load_credentials(path)
             if c.username.lower() != (username or "").strip().lower()]
    save_credentials(creds, path)
    return creds


def get_credential(username: str, path: Path | None = None) -> Credential | None:
    path = tenant.resolve(CREDENTIALS_FILE) if path is None else path
    target = (username or "").strip().lower()
    for c in load_credentials(path):
        if c.username.lower() == target:
            return c
    return None


def mask(api_key: str) -> str:
    """A display-safe rendering: fully dotted, same length as the key."""
    return "•" * len(api_key)


def seed_credentials_from_env(path: Path | None = None) -> None:
    """One-time import of a .env PROJECTX credential into the encrypted store.

    A key configured via PROJECTX_USERNAME / PROJECTX_API_KEY is otherwise only
    used as the broker fallback — it never appears in Settings. Seeding it makes it
    a managed key (listed, revealable, deletable). Mirrors `auth.seed_admin_from_env`:
    only runs in live mode and only when the store is still empty, so it never
    overrides keys the operator has added in the UI.
    """
    path = tenant.resolve(CREDENTIALS_FILE) if path is None else path
    if os.getenv("TOPHAT_BROKER", "mock").lower() != "live":
        return
    user = os.getenv("PROJECTX_USERNAME", "").strip()
    key = os.getenv("PROJECTX_API_KEY", "").strip()
    if not user or not key or load_credentials(path):
        return
    add_credential(user, key, os.getenv("PROJECTX_API_URL", ""), path)


def public_list(path: Path | None = None) -> list[dict]:
    """Credentials safe to send to the client — masked, never the raw key."""
    path = tenant.resolve(CREDENTIALS_FILE) if path is None else path
    return [
        {
            "username": c.username,
            "masked": mask(c.api_key),
            "key_length": len(c.api_key),
            "base_url": c.base_url,
            "enabled": c.enabled,
            "created_at": c.created_at,
            "readable": bool(c.api_key),
        }
        for c in load_credentials(path)
    ]
