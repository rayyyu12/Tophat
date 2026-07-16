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
  disconnected  matched account whose Tradecopia entity is offline → displayed,
                never booked
  needs_anchor  mid-phase mirror without a stored start_balance → refused
                (assuming $50k would silently corrupt the trailing floor)
  proposed      fresh, connected, known-firm account with no mirror → eligible
                for one-click operator import
  ignored       unknown account that is stale, disconnected, or cannot be
                classified safely → displayed, never importable
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


def _proposal_row(a: dict, *, now: datetime,
                  freshness_hours: float) -> dict:
    """Classify an unknown Tradecopia account for operator import.

    Import is deliberately conservative: entity connection + a recent account
    update prove that the row is live enough to offer. Copier-group membership
    does not — stale groups can survive long after an account is gone.
    """
    name = str(a.get("name") or "").strip()
    firm = _infer_firm(str(a.get("entity_organization") or ""),
                       str(a.get("entity_id") or ""))
    # firm funded prefixes: Apex "PA…", Lucid "LFF…" (Lucid evals are "LFE…")
    phase = "funded" if name.upper().startswith(("PA", "LFF")) else "eval"
    ts = _parse_ts(str(a.get("updated_at") or ""))
    age_ok = ts is not None and (now - ts) <= timedelta(hours=freshness_hours)
    connected = bool(a.get("connected"))
    try:
        balance = float(a.get("balance"))
    except (TypeError, ValueError):
        balance = None

    row = {
        "name": name,
        "firm": firm,
        "phase_hint": phase,
        "balance": balance,
        "connected": connected,
        "observed_at": ts.isoformat(timespec="seconds") if ts else "",
    }
    reasons = []
    if not connected:
        reasons.append("Tradecopia connection is offline")
    if not age_ok:
        reasons.append(f"observation is older than {freshness_hours:g} hours")
    if firm is None:
        reasons.append("firm could not be inferred")
    if balance is None:
        reasons.append("balance is not numeric")
    if reasons:
        row.update(status="ignored", importable=False, reason="; ".join(reasons))
    else:
        row.update(status="proposed", importable=True)
    return row


def import_proposed_accounts(payload: dict, *, names: list[str] | None = None,
                             freshness_hours: float = FRESHNESS_HOURS,
                             now: datetime | None = None) -> dict:
    """Create mirrors for eligible accounts from the latest observed snapshot.

    `names=None` imports every eligible proposal. A supplied list imports only
    those names. The operation is idempotent by account number.
    """
    now = now or datetime.now()
    requested = (None if names is None else
                 {str(name).strip() for name in names if str(name).strip()})
    mirrors = mirrors_store.load_mirrors()
    existing = {m.account_number for m in mirrors.values() if m.account_number}
    seen: set[str] = set()
    created = []
    skipped: list[dict] = []

    for a in payload.get("accounts") or []:
        name = str(a.get("name") or "").strip()
        if not name or (requested is not None and name not in requested):
            continue
        seen.add(name)
        if str(a.get("entity_type")) == "projectx":
            skipped.append({"name": name, "reason": "leader accounts are API-managed"})
            continue
        if name in existing:
            skipped.append({"name": name, "reason": "already imported"})
            continue
        proposal = _proposal_row(a, now=now, freshness_hours=freshness_hours)
        if proposal["status"] != "proposed":
            skipped.append({"name": name, "reason": proposal["reason"]})
            continue
        try:
            mirror = mirrors_store.create_mirror(
                proposal["firm"], account_number=name,
                phase=proposal["phase_hint"])
        except ValueError as exc:
            skipped.append({"name": name, "reason": str(exc)})
            continue
        created.append(mirror)
        existing.add(name)

    if requested is not None:
        for name in sorted(requested - seen):
            skipped.append({"name": name, "reason": "not in the latest Rabbit report"})
    return {"created": created, "skipped": skipped}


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
            results.append(_proposal_row(
                a, now=now, freshness_hours=freshness_hours))
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
        if not bool(a.get("connected")):
            row.update(status="disconnected",
                       reason="Tradecopia connection is offline")
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

    order = ("booked", "anchored", "stale", "disconnected", "needs_anchor",
             "proposed", "ignored", "skipped_leader", "error")
    summary = {k: sum(1 for r in results if r["status"] == k) for k in order}
    return {"results": results, "summary": summary}
