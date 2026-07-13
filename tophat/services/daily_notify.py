"""Server-side per-user premarket + morning-recap Discord notifications.

Replaces the retired deploy/watchdog.py's TopHat-side checks (2026-07-14):
state lives on the server's disk now, and only the server can see every
user's armed flag, trade log and webhook — so each user gets notifications
for THEIR setup, posted to THEIR Settings-page webhook, driven from the
automation loop's 30s tick.

Tradecopia status rides in on the Rabbit box heartbeat (rabbit.py POSTs
/api/ops/tc-heartbeat at startup and every ~5 min): TopHat cannot call INTO
a box behind home NAT — the architecture is pull-only — so "status at
notification time" means the box's latest report, at most minutes old, and
a silent box is itself the warning. Users with no paired box get no
Tradecopia section at all: not everyone copy-trades.

Cadence (ET, weekdays):
  premarket 09:00-09:44 — posts ONLY when something needs action before the
      open (disarmed, copier box silent/degraded). Silence = all clear.
  recap 11:00-16:00 — always posts: drive, reconciled outcomes, still-open
      trades, payout-ready accounts (assembled by service.ops_recap).
Once-per-day stamps persist per tenant (notify_state.json) so a mid-morning
deploy restart can't double-post; the stamp is written BEFORE the Discord
call so a webhook hiccup can't turn the 30s loop into a spam cannon.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from tophat.store import tenant
from tophat.store.atomic import atomic_write_text
from tophat.store.paths import NOTIFY_STATE_FILE

log = logging.getLogger("tophat.notify")

PREMARKET_HHMM = "09:00"
PREMARKET_CUTOFF = "09:44"   # entries start 09:45 - too late to act, stay quiet
RECAP_HHMM = "11:00"
RECAP_CUTOFF = "16:00"       # a recap after the session close helps nobody
HEARTBEAT_FRESH_S = 15 * 60  # rabbit posts every ~5 min; 3 misses = "silent"

GREEN, AMBER, RED = 0x2ECC71, 0xE67E22, 0xED4245


def _load_stamps() -> dict:
    try:
        return json.loads(tenant.resolve(NOTIFY_STATE_FILE)
                          .read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_stamps(st: dict) -> None:
    atomic_write_text(tenant.resolve(NOTIFY_STATE_FILE), json.dumps(st, indent=2))


def _tc_field(uid: int) -> tuple[dict | None, int]:
    """The 'Copier · Tradecopia' embed field for this user, from the freshest
    box heartbeat. (None, 0) when the user has no paired box — no copier, no
    section. Second element = number of problems."""
    from tophat.store import boxes
    if not boxes.list_boxes(uid):
        return None, 0
    hb = boxes.latest_heartbeat(uid)
    bad: list[str] = []
    ok: list[str] = []
    if hb is None or hb[1] > HEARTBEAT_FRESH_S:
        ago = "never" if hb is None else f"{hb[1] // 60} min ago"
        bad.append(f"copier box hasn't reported Tradecopia status ({ago}) — "
                   "is Rabbit running?")
    else:
        tc, _age = hb
        if not tc.get("app_running"):
            bad.append("Tradecopia is NOT RUNNING")
        if not tc.get("db_ok"):
            bad.append("Tradecopia DB unreadable")
        for c in tc.get("conns") or []:
            if not c.get("up"):
                bad.append(f"{c.get('firm', '?')} connection DOWN "
                           f"since {c.get('since') or '?'}")
        for firm, n in sorted((tc.get("feeds_bad") or {}).items()):
            try:
                bad.append(f"{firm} feeds not connected ({n[0]}/{n[1]})")
            except (TypeError, IndexError):
                bad.append(f"{firm} feeds not connected")
        if not bad:
            up = sorted({c.get("firm", "?") for c in tc.get("conns") or []
                         if c.get("up")})
            ok.append("app running"
                      + (f" · connections up: {', '.join(up)}" if up else ""))
    value = "\n".join([f"❌ {b}" for b in bad] + [f"✅ {o}" for o in ok]) or "—"
    return {"name": "Copier · Tradecopia", "value": value, "inline": True}, len(bad)


def _premarket(uid: int, settings) -> tuple[str, int, list[dict], str] | None:
    """None = all clear (post nothing — silence is the green signal)."""
    th_bad: list[str] = []
    th_ok: list[str] = []
    if settings.auto_execute:
        th_ok.append("armed (auto-execute ON)")
    else:
        th_bad.append("auto_execute is OFF — nothing fires today")
    tc_field, tc_bad = _tc_field(uid)
    if not th_bad and not tc_bad:
        return None
    fields = [{"name": "Trader · TopHat",
               "value": "\n".join([f"❌ {b}" for b in th_bad]
                                  + [f"✅ {o}" for o in th_ok]) or "—",
               "inline": True}]
    if tc_field is not None:
        fields.append(tc_field)
    return "Pre-market warning", RED, fields, "Action needed before the open."


def _recap(uid: int) -> tuple[str, int, list[dict], str]:
    from tophat.server import service
    r = service.ops_recap()
    side, src = r.get("drive") or "", r.get("drive_source") or ""
    drive = ((f"{side} ({src})" if src else side) if side
             else "not locked (flat open, or no read since 09:45 ET)")
    fields = [{"name": "Drive",
               "value": ("📈 " if side == "LONG" else
                         "📉 " if side == "SHORT" else "▪️ ") + drive,
               "inline": False}]
    lines = []
    for t in r.get("results", []):
        icon = {"win": "🎯 hit target", "loss": "🛑 hit stop"}.get(
            t.get("outcome") or "", "➖ flat")
        lines.append(f"{icon}: {t.get('account')} · {t.get('label', '?')} "
                     f"({float(t.get('pnl') or 0.0):+,.0f})")
    fields.append({"name": "Results",
                   "value": "\n".join(lines) or "no reconciled trades yet",
                   "inline": False})
    if r.get("still_open"):
        fields.append({"name": "Still open",
                       "value": "\n".join(f"⏳ {s}" for s in r["still_open"]),
                       "inline": False})
    if r.get("payout_ready"):
        fields.append({"name": "Payout ready",
                       "value": "\n".join(f"💰 {x}" for x in r["payout_ready"]),
                       "inline": False})
    tc_field, tc_bad = _tc_field(uid)
    color = GREEN
    if tc_field is not None:
        fields.append({**tc_field, "inline": False})
        if tc_bad:
            color = RED
    return "Morning recap", color, fields, ""


def maybe_send(uid: int, now_et: datetime) -> None:
    """Called from every automation tick under the user's tenant context.
    Cheap when nothing is due (one stamp read); independent of auto_execute —
    'you forgot to arm' is exactly what the premarket warning is FOR."""
    if now_et.weekday() >= 5:
        return
    from tophat.services import notify
    from tophat.store.config import load_settings
    settings = load_settings()
    url = (settings.discord_webhook_url or "").strip()
    if not url:
        return
    hhmm = now_et.strftime("%H:%M")
    today = now_et.strftime("%Y-%m-%d")
    stamps = _load_stamps()
    if PREMARKET_HHMM <= hhmm < PREMARKET_CUTOFF and stamps.get("premarket") != today:
        kind, built = "premarket", _premarket(uid, settings)
    elif RECAP_HHMM <= hhmm < RECAP_CUTOFF and stamps.get("recap") != today:
        kind, built = "recap", _recap(uid)
    else:
        return
    stamps[kind] = today
    _save_stamps(stamps)
    if built is None:
        log.info("premarket all clear uid=%s — nothing posted", uid)
        return
    title, color, fields, desc = built
    try:
        notify.post_discord(url, title, color=color, fields=fields,
                            description=desc, footer="TopHat")
        log.info("%s posted uid=%s", kind, uid)
    except Exception as exc:
        log.warning("%s post failed uid=%s: %s", kind, uid, exc)
