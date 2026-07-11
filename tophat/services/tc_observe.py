"""Reverse-sync importer: observed Tradecopia account facts → mirror bookkeeping
(docs/TRADECOPIA_AUTOMATION_PLAN.md §13.4). TopHat owns ALL policy — the box
copies rows verbatim; this module decides what to trust and what to book.

Per-account outcomes:
  booked        fresh observation, anchor known → equity synced via the same
                path as a manual dashboard sync (patch_mirror sync_balance)
  anchored      fresh phase (untraded): first observation captured as the
                start_balance anchor and booked at equity 0 (§13.4.1)
  stale         observation older than the freshness window → displayed, never
                booked (a six-week-stale row must not reset a mirror)
  needs_anchor  mid-phase mirror without a stored start_balance → refused
                (assuming $50k would silently corrupt the trailing floor)
  proposed      no mirror has this account number → onboarding proposal for the
                operator (never auto-created)
  skipped_leader / error — self-describing

Deviation from the §13.4 sketch, on purpose: observations (updated_at,
connected, day/week P&L) are stored in the per-tenant snapshot file rather than
as new mirror fields — `last_day_pnl` feeds the solver's nuke-landed detection
and must never be written from a second source.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tophat.store import mirrors as mirrors_store

FRESHNESS_HOURS = 36.0   # §13.4.2 default: book only observations younger than this

# entities.organization → FirmProfile.key (onboarding proposals; lowercase match)
_ORG_FIRM = {"apex": "apex-50k", "lucid": "lucid-50k", "tradeify": "tradeify-50k"}


def _parse_ts(s: str) -> datetime | None:
    """Tolerant parse of Tradecopia timestamps ('2026-05-28 19:40:52.8462114-05:00',
    '2026-05-29 22:19:47', ISO 'T' variants). Returns naive local time; None when
    unparseable (callers treat that as stale — never trust what you can't date)."""
    s = str(s or "").strip().replace("T", " ", 1)
    if not s:
        return None
    # normalize a 7-digit fraction (Go) down to 6 for fromisoformat
    if "." in s:
        head, _, tail = s.partition(".")
        frac = ""
        for ch in tail:
            if ch.isdigit():
                frac += ch
            else:
                break
        rest = tail[len(frac):]
        s = f"{head}.{frac[:6].ljust(6, '0')}{rest}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _infer_firm(entity_org: str, entity_id: str) -> str | None:
    hay = f"{entity_org} {entity_id}".lower()
    for needle, key in _ORG_FIRM.items():
        if needle in hay:
            return key
    return None


def import_observed(payload: dict, *, today: str,
                    freshness_hours: float = FRESHNESS_HOURS,
                    now: datetime | None = None) -> dict:
    """Apply one observed snapshot (§13.3 shape). Returns
    {"results": [...], "summary": {...}} and books eligible balances through
    the mirrors store. Never creates or deletes mirrors."""
    now = now or datetime.now()
    mirrors = mirrors_store.load_mirrors()
    by_number: dict[str, list] = {}
    for m in mirrors.values():
        if m.account_number:
            by_number.setdefault(m.account_number, []).append(m)

    results: list[dict] = []
    for a in payload.get("accounts") or []:
        name = str(a.get("name") or "").strip()
        if not name:
            continue
        row: dict = {"name": name}
        if str(a.get("entity_type")) == "projectx":
            row["status"] = "skipped_leader"
            results.append(row)
            continue

        matches = by_number.get(name, [])
        if len(matches) > 1:
            row.update(status="error",
                       reason=f"{len(matches)} mirrors share account number {name}")
            results.append(row)
            continue
        if not matches:
            row.update(status="proposed",
                       firm=_infer_firm(str(a.get("entity_organization") or ""),
                                        str(a.get("entity_id") or "")),
                       balance=a.get("balance"),
                       phase_hint=("funded" if name.upper().startswith("PA")
                                   else "eval"))
            results.append(row)
            continue

        m = matches[0]
        row["mirror_id"] = m.mirror_id
        ts = _parse_ts(str(a.get("updated_at") or ""))
        age_ok = ts is not None and (now - ts) <= timedelta(hours=freshness_hours)
        row["observed_at"] = ts.isoformat(timespec="seconds") if ts else ""
        if not age_ok:
            row["status"] = "stale"
            results.append(row)
            continue

        try:
            balance = float(a.get("balance"))
        except (TypeError, ValueError):
            row.update(status="error", reason="no numeric balance")
            results.append(row)
            continue

        if m.start_balance is None:
            fresh_phase = (m.days_traded == 0 and abs(m.equity) < 1e-9)
            if not fresh_phase:
                row.update(status="needs_anchor",
                           reason="mid-phase mirror has no start_balance anchor - "
                                  "set it on the Accounts page (balance at phase start)")
                results.append(row)
                continue
            mirrors_store.patch_mirror(
                m.mirror_id, {"start_balance": balance, "sync_balance": 0.0},
                today=today)
            row.update(status="anchored", start_balance=balance, equity=0.0)
            results.append(row)
            continue

        equity = round(balance - float(m.start_balance), 2)
        mirrors_store.patch_mirror(m.mirror_id, {"sync_balance": equity},
                                   today=today)
        row.update(status="booked", equity=equity, balance=balance)
        results.append(row)

    order = ("booked", "anchored", "stale", "needs_anchor", "proposed",
             "skipped_leader", "error")
    summary = {k: sum(1 for r in results if r["status"] == k) for k in order}
    return {"results": results, "summary": summary}
