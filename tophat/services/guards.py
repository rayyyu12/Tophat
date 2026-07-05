"""Pre-trade safety guards.

Topstep's no-hedging rule is per-account (not per-fleet) — two different accounts
holding opposite positions is fine and good for decorrelation. The only guard we
need is: never send an entry to an account that already has an open position
(covers both the opposite-direction hedge case and DLL-breaching pyramiding).
"""

from __future__ import annotations


def position_guard(broker, account_id: int, contract_id: str) -> tuple[bool, str]:
    """Return (ok_to_trade, reason). ok=False means skip this account today."""
    try:
        positions = broker.search_open_positions(account_id)
    except Exception as exc:  # never trade blind if we can't read positions
        return False, f"position check failed: {exc}"
    open_on_contract = [
        p for p in positions
        if (contract_id is None or p.get("contractId") == contract_id)
        and int(p.get("size", 0)) != 0
    ]
    if open_on_contract:
        return False, "account not flat - skipped to avoid hedge/pyramid"
    return True, ""
