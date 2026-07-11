"""Exporter: the day's APPLIED copier mapping → the tc_desired_state payload
(docs/TRADECOPIA_AUTOMATION_PLAN.md §5.1, v2) that TopHat Rabbit pulls.

Source of truth is the mirrors store — which is only current once today's
copier plan has been applied (apply_plan persists the desired mappings). So
the export refuses while the freshly-solved plan still contains copier-edit
lines: exporting a stale arrangement to an automated writer would happily
rearrange Tradecopia into yesterday's world.

v2 contract note (differs from the original §5.1 sketch): the `guard` block
(goose version + Tradecopia user id) lives in the BOX's rabbit_config.json —
they are facts about one host, which the hosted TopHat cannot know. The file
instead carries `tenant`, and the box refuses to apply a payload whose tenant
differs from its configured pairing (enforced server-side by the box token
binding anyway).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.store import tenant
from tophat.store.mirrors import load_mirrors

# Plan lines that mean "the operator has not applied today's copier edits yet".
EDIT_ACTIONS = {"MAP", "UNMAP", "MOVE", "SET_MULT", "SET_CHANNEL"}
ET = ZoneInfo("America/New_York")


def desired_from_mirrors(mirrors: dict, names: dict[int, str],
                         pending_edits: int, today: str) -> dict:
    """Pure assembly + validation. Raises ValueError with an operator-readable
    reason (surfaced on the Operations page / returned as HTTP 409)."""
    if pending_edits:
        raise ValueError(f"today's copier plan has {pending_edits} unapplied "
                         "line(s) - apply the plan first, then the export matches "
                         "the mirrors store")
    # Copy trading not started (or the whole fleet retired): refuse instead of
    # serving an empty world - an empty export would make a running Rabbit
    # quit/relaunch Tradecopia just to delete leftovers, on a day when the
    # operator only has leader accounts. Mirrors that EXIST but are unmapped
    # still export as [] (a deliberate unmap-everything is legitimate).
    if not any(m.enabled and not m.terminal for m in mirrors.values()):
        raise ValueError("no active mirror (follower) accounts - copy trading "
                         "is idle; add mirrors on the Accounts page when ready")
    groups: dict[int, list[dict]] = {}
    seen_accounts: dict[str, str] = {}
    for mid in sorted(mirrors):
        m = mirrors[mid]
        if not m.enabled or m.terminal or m.leader_id is None:
            continue
        if m.phase not in ("eval", "funded"):
            continue    # passed/waiting are unmapped by definition
        if not m.account_number:
            raise ValueError(f"mirror {m.alias or mid} has no account number - "
                             "set it on the Accounts page before exporting")
        if m.account_number in seen_accounts:
            raise ValueError(f"account number {m.account_number} is on two "
                             f"mirrors ({seen_accounts[m.account_number]} and {mid})")
        seen_accounts[m.account_number] = mid
        groups.setdefault(int(m.leader_id), []).append({
            "account": m.account_number,
            "scale": float(m.multiplier),
            "replicate": True,
            "contract_type": "Standard",
        })
    out_groups = []
    for lid in sorted(groups, key=lambda x: (names.get(x) or "", x)):
        name = names.get(lid, "")
        if not name:
            raise ValueError(f"cannot resolve leader account #{lid} to a broker "
                             "name - is its API key still connected?")
        out_groups.append({"leader": name,
                           "followers": sorted(groups[lid],
                                               key=lambda f: f["account"])})
    return {
        "version": 2,
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "plan_date": today,
        "tenant": tenant.get_user(),
        "groups": out_groups,
    }


def build_desired_state(pool, today: str) -> dict:
    """Live assembly: solve today's plan (for the freshness check), resolve
    leader names from snapshots, and export the mirrors store."""
    from tophat.server import service
    from tophat.services import copier_plan as cp
    plan = cp.build_today_plan(pool, today)
    pending = sum(1 for l in plan.lines if l.action in EDIT_ACTIONS)
    mirrors = load_mirrors()
    names = service.account_names(pool)
    return desired_from_mirrors(mirrors, names, pending, today)
