"""Run a trading session (dry-run or execute) for selected accounts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from tophat.broker.projectx.broker import ProjectXBroker
from tophat.engine import (
    AccountConfig, AccountState, Action, Phase, decide, should_omit_stop, start_new_day)
from tophat.services.status import infer_phase_from_name, is_practice, lifecycle_label
from tophat.store.registry import AccountRegistry
from tophat.store.states import get_or_create, load_all, save_all


@dataclass
class AccountResult:
    account_id: int
    name: str
    enabled: bool
    phase: str
    lifecycle: str
    balance: float
    action: str
    plan_label: str = ""
    plan_side: str = ""
    contracts: int = 0
    note: str = ""
    order_id: int | None = None
    skipped: str = ""


@dataclass
class RunSummary:
    drive: int
    drive_source: str
    nq_contract: str
    results: list[AccountResult] = field(default_factory=list)
    orders_placed: int = 0


def sync_balance(state: AccountState, balance: float) -> None:
    state.equity = balance
    state.peak_equity_eod = max(state.peak_equity_eod, balance)


def _open_size(broker, account_id: int, contract_id: str) -> int:
    """Net open contracts (0 = flat). Tolerant — never raises into the trading path."""
    try:
        return sum(int(p.get("size", 0)) for p in broker.search_open_positions(account_id)
                   if contract_id is None or p.get("contractId") == contract_id)
    except Exception:
        return 0


def run_session(
    broker: ProjectXBroker,
    *,
    drive: int,
    drive_source: str,
    registry: AccountRegistry,
    account_filter: set[int] | None = None,
    execute: bool = False,
    confirm_nukes: bool = False,
    bootstrap_phase: str | None = None,
) -> RunSummary:
    cfg = AccountConfig()
    states = load_all()
    nq = broker.resolve_nq_contract()
    accounts = broker.list_accounts()
    summary = RunSummary(drive=drive, drive_source=drive_source, nq_contract=nq)

    for acct in accounts:
        # Practice accounts report canTrade=true but must never fire from the
        # legacy runner (no signal/manual-validation concept here — see
        # server.service.run_session for those paths).
        if not acct.can_trade or is_practice(acct.name):
            continue
        if account_filter and acct.account_id not in account_filter:
            continue
        if not registry.is_enabled(acct.account_id):
            continue

        is_new = acct.account_id not in states
        state = get_or_create(states, acct.account_id)
        if is_new and bootstrap_phase:
            state.phase = Phase(bootstrap_phase)
        elif is_new:
            state.phase = infer_phase_from_name(acct.name)

        start_new_day(state)
        sync_balance(state, acct.balance)
        dec = decide(cfg, state, drive)

        res = AccountResult(
            account_id=acct.account_id,
            name=registry.entry(acct.account_id).alias or acct.name,
            enabled=True,
            phase=state.phase.value,
            lifecycle=lifecycle_label(cfg, state),
            balance=acct.balance,
            action=dec.action.value,
            note=dec.note,
        )

        if dec.action == Action.TRADE and dec.plan:
            res.plan_label = dec.plan.label
            res.plan_side = "LONG" if dec.plan.direction == 1 else "SHORT"
            res.contracts = dec.plan.contracts

            if execute:
                is_nuke = dec.plan.label in ("nuke", "renuke")
                if is_nuke and not confirm_nukes:
                    res.skipped = "nuke requires confirm"
                else:
                    plan = dec.plan
                    if should_omit_stop(cfg, state, plan, acct.balance):
                        plan = replace(plan, manual_stop=False)
                        res.note = (res.note + " | no stop: near floor").strip(" |")
                    # Flatten only if a position exists — closing a flat account errors
                    # on ProjectX ("error 2") and would abort the run.
                    if _open_size(broker, acct.account_id, nq):
                        broker.close_contract(acct.account_id, nq)
                    res.order_id = broker.place_bracket(acct.account_id, nq, plan)
                    summary.orders_placed += 1

        summary.results.append(res)

    save_all(states)
    return summary
