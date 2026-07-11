"""Nightly Auto-OCO Brackets probe (operator-designed 2026-07-09).

Topstep's "Auto OCO Brackets" is a per-account platform setting with no API
read — the only detector is the order-place rejection ('error 2: Brackets
cannot be used with Position Brackets'). Waiting for the morning fire to hit
that rejection costs the account its trading day, so every night at
settings.oco_probe_time (default 22:00 ET = 21:00 CT, evening session open)
this probe places a far-below-market 1-lot bracketed limit order on every
account the strategy could actually trade tomorrow — enabled, broker-tradeable,
not practice, not terminal, and not dead-by-balance (below its trailing MLL
floor: Topstep sometimes keeps canTrade=true on those; the dashboard already
shows them "inactive", 2026-07-09) — and cancels it immediately:

    accepted  -> Auto OCO is ON (order cancelled, nothing rests)
    error 2   -> Auto OCO is OFF -> Discord alert names the account
    other     -> reported as "couldn't check" in the same alert

Runs Sun–Thu nights only (CME evening session; Fri/Sat the market is closed).
Quiet when every account passes — the alert exists to be actionable.

Copy-leader accounts are NEVER probed: Tradecopia replicates a leader's orders
to API-less followers, so even a place-and-cancel probe could leave a resting
order on a follower if the copied cancel hiccups. Leaders fire a real bracket
every trading day, so their OCO state is proven (or alerted) by the live fire
path instead. If the Gate A supervised run shows Tradecopia does NOT replicate
unfilled resting orders, this exclusion can be dropped.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from tophat.engine import TERMINAL
from tophat.services.status import is_practice
from tophat.store import tenant
from tophat.store.atomic import atomic_write_text
from tophat.store.config import load_settings
from tophat.store.paths import OCO_PROBE_FILE
from tophat.store.registry import load_registry, save_registry
from tophat.store.states import load_all

log = logging.getLogger("tophat.oco_probe")

# 22:00 ET nights when the CME evening session is open: Sun(6)–Thu(3).
PROBE_WEEKDAYS = {6, 0, 1, 2, 3}
_PAUSE_BETWEEN_ACCOUNTS_S = 0.3   # stay far below the gateway's burst limits


def probe_due(now_et: datetime, probe_time: str, last_done: str) -> bool:
    """One shot per calendar night, at/after probe_time, session-open nights only."""
    if not probe_time:
        return False
    today = now_et.strftime("%Y-%m-%d")
    return (last_done != today
            and now_et.weekday() in PROBE_WEEKDAYS
            and now_et.strftime("%H:%M") >= probe_time)


def load_stamp() -> str:
    """Last completed probe night (YYYY-MM-DD). Persisted per tenant so a server
    restart after the probe can't re-probe (and re-alert) the same night."""
    p = tenant.resolve(OCO_PROBE_FILE)
    if not p.exists():
        return ""
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("last_done", ""))
    except (OSError, ValueError):
        return ""


def save_stamp(day: str) -> None:
    atomic_write_text(tenant.resolve(OCO_PROBE_FILE),
                      json.dumps({"last_done": day}))


def run_probe(pool, *, now_et: datetime | None = None) -> dict:
    """Probe every enabled, non-terminal account across all credentials.

    Returns {"checked": n, "off": [...], "errors": [...], "posted": bool};
    each off/error item is {"owner","account_id","name","detail"}. Persists
    oco_blocked_on for OFF accounts (ops history) and clears it on a pass.
    """
    settings = load_settings()
    cfg = settings.to_account_config()
    states = load_all()
    registry = load_registry()   # read-only snapshot for the enabled/alias checks
    today = (now_et or datetime.now()).strftime("%Y-%m-%d")
    checked = 0
    off: list[dict] = []
    ok_ids: list[int] = []
    errors: list[dict] = []
    # Current Tradecopia copy leaders (mapped mirror leaders): never probed.
    from tophat.server.service import _below_mll
    from tophat.store.mirrors import load_mirrors
    copy_leader_ids = {m.leader_id for m in load_mirrors().values()
                       if m.enabled and not m.terminal and m.leader_id is not None}

    for h in pool:
        if h.broker is None:
            continue
        try:
            contract = h.broker.resolve_nq_contract()
            accounts = h.broker.list_accounts()
        except Exception as exc:
            errors.append({"owner": h.owner, "account_id": 0, "name": h.owner,
                           "detail": f"broker unavailable: {exc}"})
            continue
        for a in accounts:
            entry = registry.entry(a.account_id)
            st = states.get(a.account_id)
            terminal = st is not None and st.phase in TERMINAL
            if not entry.enabled or not a.can_trade or entry.force_inactive or terminal:
                continue
            if entry.signal_plan or a.account_id in copy_leader_ids:
                continue   # copy leaders: probe orders would replicate to followers
            if is_practice(a.name):
                continue   # practice accounts never fire strategy orders
            if st is not None and _below_mll(cfg, st, a.balance):
                continue   # dead by balance (broker may still say canTrade=true;
                           # the dashboard already shows these as "inactive")
            status, detail = h.broker.check_oco_bracket_support(a.account_id, contract)
            checked += 1
            row = {"owner": h.owner, "account_id": a.account_id,
                   "name": entry.alias or a.name, "detail": detail}
            if status == "off":
                off.append(row)
            elif status == "error":
                errors.append(row)
                if detail.startswith("no recent bars"):
                    # Market closed (holiday): every account will say the same —
                    # stop probing this credential, report once.
                    break
            else:
                ok_ids.append(a.account_id)
            time.sleep(_PAUSE_BETWEEN_ACCOUNTS_S)

    _persist_flags([r["account_id"] for r in off], ok_ids, today)
    posted = False
    if (off or errors) and settings.discord_webhook_url:
        posted = _post_alert(settings.discord_webhook_url, off, errors)
    log.info("OCO probe done — checked=%d off=%d errors=%d posted=%s",
             checked, len(off), len(errors), posted)
    return {"checked": checked, "off": off, "errors": errors, "posted": posted}


def _persist_flags(off_ids: list[int], ok_ids: list[int], today: str) -> None:
    """Stamp/clear oco_blocked_on under the service's registry lock — the probe
    loop is slow (one REST round-trip per account), so it must not hold a
    registry object across the run and save it wholesale over concurrent edits."""
    if not off_ids and not ok_ids:
        return
    from tophat.server.service import _REG_LOCK, invalidate_snapshot_cache
    with _REG_LOCK:
        reg = load_registry()
        for aid in off_ids:
            reg.entry(aid).oco_blocked_on = today
        for aid in ok_ids:
            reg.entry(aid).oco_blocked_on = ""
        save_registry(reg)
    invalidate_snapshot_cache()


def _post_alert(webhook: str, off: list[dict], errors: list[dict]) -> bool:
    from tophat.services.notify import post_discord
    parts = []
    if off:
        lines = "\n".join(f"- **{r['name']}** (#{r['account_id']}, {r['owner']})"
                          for r in off)
        parts.append("**Auto OCO Brackets is OFF** — these accounts will reject "
                     f"tomorrow's orders:\n{lines}\n"
                     "Fix: Topstep platform → account settings → enable "
                     "**Auto OCO Brackets**.")
    if errors:
        lines = "\n".join(f"- {r['name']} (#{r['account_id']}, {r['owner']}): "
                          f"{r['detail'][:160]}" for r in errors)
        parts.append(f"Couldn't check:\n{lines}")
    try:
        post_discord(webhook, "🌙 Nightly OCO check", description="\n\n".join(parts),
                     color=0xE74C3C if off else 0xF39C12)
        return True
    except Exception as exc:
        log.warning("OCO probe Discord post failed: %s", exc)
        return False
