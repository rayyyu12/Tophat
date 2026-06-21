"""Run a trading session (dry-run or execute) for selected accounts."""

from __future__ import annotations

from dataclasses import dataclass, field

from tophat.broker.projectx.broker import ProjectXBroker
from tophat.engine import AccountConfig, AccountState, Action, Phase, decide, start_new_day
from tophat.services.status import infer_phase_from_name, lifecycle_label
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
        if not acct.can_trade:
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
                    broker.close_contract(acct.account_id, nq)
                    res.order_id = broker.place_bracket(acct.account_id, nq, dec.plan)
                    summary.orders_placed += 1

        summary.results.append(res)

    save_all(states)
    return summary
