"""Typer CLI with Rich output — backup to the dashboard UI."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from tophat.broker.projectx.broker import ProjectXBroker
from tophat.broker.projectx.client import ProjectXError
from tophat.cli.theme import PANEL_BORDER, TOPHAT_THEME
from tophat.services.runner import run_session
from tophat.store.registry import load_registry

app = typer.Typer(
    name="tophat",
    help="TopHat NQ automation — dashboard: python -m tophat | CLI: probe, run",
    add_completion=False,
)
console = Console(theme=TOPHAT_THEME)
ET = ZoneInfo("America/New_York")


def _require_env() -> None:
    if not os.getenv("PROJECTX_USERNAME") or not os.getenv("PROJECTX_API_KEY"):
        console.print("[err]Set PROJECTX_USERNAME and PROJECTX_API_KEY in .env[/err]")
        raise typer.Exit(1)


def _parse_direction(s: str) -> int:
    m = {"long": 1, "short": -1, "flat": 0}
    return m[s.lower()]


@app.command()
def probe(
    drive: bool = typer.Option(False, "--drive", help="Compute drive from REST bars"),
) -> None:
    """Test API auth, list accounts, resolve NQ contract."""
    load_dotenv()
    _require_env()
    broker = ProjectXBroker()
    try:
        broker.login()
        console.print("[ok]auth OK[/ok]")
        accounts = broker.list_accounts()
        table = Table(title="Accounts", border_style=PANEL_BORDER)
        table.add_column("ID", style="id")
        table.add_column("Name")
        table.add_column("Balance", justify="right", style="money")
        table.add_column("Trade")
        table.add_column("Type")
        for a in accounts:
            table.add_row(
                str(a.account_id), a.name, f"${a.balance:,.2f}",
                "[ok]yes[/ok]" if a.can_trade else "[muted]no[/muted]",
                "sim" if a.simulated else "live",
            )
        console.print(table)
        nq = broker.resolve_nq_contract()
        console.print(f"[banner]Active NQ:[/banner] {nq}")
        if drive:
            d = broker.drive_direction(nq)
            label = {1: "[long]LONG[/long]", -1: "[short]SHORT[/short]",
                     0: "[flat]FLAT[/flat]"}[d]
            console.print(f"[banner]Drive (REST bars):[/banner] {label}")
    except ProjectXError as exc:
        console.print(f"[err]{exc}[/err]")
        raise typer.Exit(1)
    finally:
        broker.close()


@app.command("run")
def run_cmd(
    accounts: Optional[list[int]] = typer.Option(None, "--accounts", help="Account IDs"),
    direction: Optional[str] = typer.Option(None, "--direction", help="long|short|flat"),
    execute: bool = typer.Option(False, "--execute", help="Place orders"),
    confirm_nukes: bool = typer.Option(False, "--confirm-nukes", help="Allow nukes"),
    bootstrap_phase: Optional[str] = typer.Option(
        None, "--bootstrap-phase", help="eval|funded for new accounts"),
) -> None:
    """Dry-run or execute today's plan for enabled accounts."""
    load_dotenv()
    _require_env()
    registry = load_registry()
    broker = ProjectXBroker()
    try:
        broker.login()
        nq = broker.resolve_nq_contract()
        if direction:
            drive = _parse_direction(direction)
            src = f"override ({direction})"
        else:
            drive = broker.drive_direction(nq)
            src = "REST bars 09:30-09:45"
        filt = set(accounts) if accounts else None
        summary = run_session(
            broker, drive=drive, drive_source=src, registry=registry,
            account_filter=filt, execute=execute, confirm_nukes=confirm_nukes,
            bootstrap_phase=bootstrap_phase,
        )
        now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
        mode = "EXECUTE" if execute else "DRY-RUN"
        console.print(Panel(
            f"[banner]{mode}[/banner]  {now}\n"
            f"NQ {summary.nq_contract}\n"
            f"Drive {summary.drive:+d} ({summary.drive_source})",
            title="TopHat Runner", border_style=PANEL_BORDER,
        ))
        if not summary.results:
            console.print("[warn]No enabled tradeable accounts. Enable accounts in the dashboard (E).[/warn]")
            return
        table = Table(border_style=PANEL_BORDER)
        table.add_column("Account")
        table.add_column("Lifecycle")
        table.add_column("Balance", justify="right", style="money")
        table.add_column("Today")
        for r in summary.results:
            today = r.action.upper()
            if r.plan_label:
                style = "nuke" if r.plan_label in ("nuke", "renuke") else "flip"
                today = f"[{style}]{r.plan_label}[/] {r.plan_side} x{r.contracts}"
            if r.skipped:
                today += f" [warn]({r.skipped})[/warn]"
            if r.order_id:
                today += f" [ok]#{r.order_id}[/ok]"
            table.add_row(r.name, r.lifecycle, f"${r.balance:,.2f}", today)
        console.print(table)
        console.print(f"[muted]Orders placed: {summary.orders_placed}[/muted]")
        if not execute:
            console.print("[muted]Dry-run — add --execute to place orders[/muted]")
    except ProjectXError as exc:
        console.print(f"[err]{exc}[/err]")
        raise typer.Exit(1)
    finally:
        broker.close()


def cli_entry() -> None:
    load_dotenv()
    app()


if __name__ == "__main__":
    cli_entry()
