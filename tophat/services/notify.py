"""Server-side Discord notifications via the per-user webhook (Settings page,
TopHatSettings.discord_webhook_url).

One tiny embed poster shared by anything in the server that pings a user's
channel — the Settings "send test" button, the nightly copier (Rabbit) apply
results, fire-failure alerts, the OCO probe, and the daily premarket/recap
notices (services/daily_notify.py).
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone


def post_discord(webhook_url: str, title: str, *, description: str = "",
                 color: int = 0x2ECC71, fields: list[dict] | None = None,
                 footer: str = "TopHat") -> None:
    """POST one embed. Raises on any failure — callers decide how loud to be."""
    embed: dict = {
        "title": title,
        "color": color,
        "footer": {"text": footer},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if description:
        embed["description"] = description
    if fields:
        embed["fields"] = fields
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps({"embeds": [embed]}).encode(),
        headers={"Content-Type": "application/json",
                 # Cloudflare 403s the default Python-urllib UA
                 "User-Agent": "TopHat/1.0"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"discord {exc.code}: {body}") from None
