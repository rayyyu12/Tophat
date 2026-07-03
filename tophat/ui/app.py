"""TopHat terminal dashboard (Textual TUI)."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Label, Static

from tophat.broker.base import BrokerAccount
from tophat.broker.projectx.broker import ProjectXBroker
from tophat.broker.projectx.client import ProjectXError
from tophat.engine import AccountConfig, Action, decide, start_new_day
from tophat.services.runner import run_session
from tophat.services.status import infer_phase_from_name, lifecycle_label
from tophat.store.registry import load_registry, save_registry
from tophat.store.states import get_or_create, load_all, save_all
from tophat.streaming.manager import StreamManager

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
STREAM_UI_REFRESH_SEC = 0.5


def _now_ct() -> str:
    return datetime.now(CT).strftime("%H:%M CT")


class SettingsScreen(ModalScreen[bool]):
    """Toggle global settings."""

    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-panel {
        width: 60;
        height: auto;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    """

    BINDINGS = [("escape", "dismiss(False)", "Close")]

    def compose(self) -> ComposeResult:
        reg = self.app.registry  # type: ignore[attr-defined]
        with Container(id="settings-panel"):
            yield Label("[bold]Settings[/bold]")
            yield Label(f"Confirm nukes before execute: {reg.settings.confirm_nukes}")
            yield Label(f"Show disabled accounts: {reg.settings.show_disabled}")
            yield Label("")
            yield Label("[dim]N — toggle confirm nukes[/dim]")
            yield Label("[dim]V — toggle show disabled[/dim]")
            yield Label("[dim]Esc — close[/dim]")

    def action_dismiss(self, value: bool = False) -> None:
        self.dismiss(value)

    def key_n(self) -> None:
        self.app.registry.settings.confirm_nukes = not self.app.registry.settings.confirm_nukes  # type: ignore
        save_registry(self.app.registry)  # type: ignore
        self.app.pop_screen()
        self.app.push_screen(SettingsScreen())

    def key_v(self) -> None:
        self.app.registry.settings.show_disabled = not self.app.registry.settings.show_disabled  # type: ignore
        save_registry(self.app.registry)  # type: ignore
        self.app.pop_screen()
        self.app.push_screen(SettingsScreen())


class TopHatApp(App):
    """Live dashboard with SignalR streaming."""

    TITLE = "TopHat NQ"
    CSS = """
    Screen { background: $surface; }
    #status-bar {
        height: 3;
        padding: 0 1;
        background: $boost;
        color: $text;
    }
    #accounts-table { height: 1fr; }
    .status-ok { color: $success; }
    .status-warn { color: $warning; }
    .status-err { color: $error; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("e", "toggle_enabled", "Enable/Disable"),
        Binding("d", "dry_run", "Dry-run"),
        Binding("x", "execute", "Execute"),
        Binding("s", "settings", "Settings"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.broker: ProjectXBroker | None = None
        self.stream: StreamManager | None = None
        self.registry = load_registry()
        self.states = load_all()
        self.cfg = AccountConfig()
        self.nq_contract = ""
        self._accounts: list[BrokerAccount] = []
        self._accounts_by_row: dict[int, int] = {}
        self._last_stream_refresh = 0.0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status-bar")
        yield DataTable(id="accounts-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#accounts-table", DataTable)
        table.add_columns(
            "On", "Account", "Phase", "Lifecycle", "Balance",
            "Position", "Today's Plan", "Drive",
        )
        self._bootstrap()

    @work(thread=True)
    def _bootstrap(self) -> None:
        try:
            if not os.getenv("PROJECTX_USERNAME") or not os.getenv("PROJECTX_API_KEY"):
                self.call_from_thread(self._set_status, "Missing .env credentials", error=True)
                return
            broker = ProjectXBroker()
            broker.login()
            nq = broker.resolve_nq_contract()
            token = broker.client.token
            rtc = broker.client.rtc_url
            accounts = broker.list_accounts()
            for a in accounts:
                self.registry.entry(a.account_id)
            save_registry(self.registry)

            stream = StreamManager(token or "", rtc, nq)
            stream.set_account_ids({a.account_id for a in accounts})
            stream.on_change(self._on_stream_update)
            stream.start()

            # seed stream snapshot with REST balances
            from tophat.streaming.manager import AccountLive
            with stream._lock:
                for a in accounts:
                    stream.snapshot.accounts[a.account_id] = AccountLive(
                        account_id=a.account_id, name=a.name, balance=a.balance,
                        can_trade=a.can_trade, simulated=a.simulated,
                    )

            self.call_from_thread(
                lambda: self._finish_bootstrap(broker, stream, nq, accounts))
        except Exception as exc:
            self.call_from_thread(self._set_status, f"Bootstrap failed: {exc}", error=True)

    def _finish_bootstrap(self, broker, stream, nq, accounts) -> None:
        self.broker = broker
        self.stream = stream
        self.nq_contract = nq
        self._accounts = list(accounts)
        for a in accounts:
            if a.account_id not in self.states:
                st = get_or_create(self.states, a.account_id)
                st.phase = infer_phase_from_name(a.name)
        save_all(self.states)
        self._refresh_table()
        self._set_status("Connected — streaming live quotes & account updates")

    def _on_stream_update(self) -> None:
        """Refresh UI from stream snapshot — no REST calls."""
        now = time.monotonic()
        if now - self._last_stream_refresh < STREAM_UI_REFRESH_SEC:
            return
        self._last_stream_refresh = now
        self.call_from_thread(self._refresh_table)

    def _set_status(self, msg: str, *, error: bool = False) -> None:
        bar = self.query_one("#status-bar", Static)
        cls = "status-err" if error else "status-ok"
        bar.update(f"[{cls}]{msg}[/]")

    def _drive(self) -> int:
        if self.stream:
            with self.stream._lock:
                return self.stream.snapshot.drive.direction
        return 0

    def _drive_label(self) -> str:
        if self.stream:
            with self.stream._lock:
                return self.stream.snapshot.drive.status_label
        return "?"

    def _refresh_table(self) -> None:
        if not self.broker or not self.stream:
            return
        table = self.query_one("#accounts-table", DataTable)
        table.clear()
        self._accounts_by_row.clear()

        drive = self._drive()
        with self.stream._lock:
            snap = self.stream.snapshot
            last_px = snap.last_price
            mkt = snap.market_connected
            usr = snap.user_connected
            qn = snap.quote_updates
            live_accts = dict(snap.accounts)

        now = _now_ct()
        px = f"{last_px:,.2f}" if last_px is not None else "—"
        hdr = (
            f"NQ {self.nq_contract}  |  "
            f"Last {px}  |  Drive {self._drive_label()}  |  "
            f"Stream M{'OK' if mkt else '—'}/U{'OK' if usr else '—'}  |  "
            f"Quotes {qn}  |  {now}"
        )
        self.query_one("#status-bar", Static).update(hdr)

        row = 0
        for acct in self._accounts:
            live = live_accts.get(acct.account_id)
            can_trade = live.can_trade if live else acct.can_trade
            if not can_trade and not self.registry.settings.show_disabled:
                continue
            entry = self.registry.entry(acct.account_id)
            if not entry.enabled and not self.registry.settings.show_disabled:
                continue

            balance = live.balance if live else acct.balance
            pos = live.open_position if live else 0

            state = get_or_create(self.states, acct.account_id)
            start_new_day(state)
            state.equity = balance
            dec = decide(self.cfg, state, drive)

            plan = ""
            if dec.action == Action.TRADE and dec.plan:
                side = "L" if dec.plan.direction == 1 else "S"
                plan = f"{dec.plan.label} {side}x{dec.plan.contracts}"
            else:
                plan = dec.action.value

            on = "[green]Y[/]" if entry.enabled else "[red]N[/]"
            name = entry.alias or acct.name
            table.add_row(
                on, name[:28], state.phase.value,
                lifecycle_label(self.cfg, state),
                f"${balance:,.0f}", str(pos), plan, self._drive_label(),
                key=str(acct.account_id),
            )
            self._accounts_by_row[row] = acct.account_id
            row += 1

    def _selected_account_id(self) -> int | None:
        table = self.query_one("#accounts-table", DataTable)
        if table.cursor_row is None:
            return None
        return self._accounts_by_row.get(table.cursor_row)

    def action_refresh(self) -> None:
        self._reload_accounts()

    @work(thread=True)
    def _reload_accounts(self) -> None:
        if not self.broker:
            return
        try:
            accounts = self.broker.list_accounts()
            self.call_from_thread(self._apply_accounts, accounts)
        except ProjectXError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")

    def _apply_accounts(self, accounts: list[BrokerAccount]) -> None:
        self._accounts = list(accounts)
        self._refresh_table()

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen())

    def _account_name(self, account_id: int) -> str:
        for acct in self._accounts:
            if acct.account_id == account_id:
                return self.registry.entry(account_id).alias or acct.name
        return str(account_id)

    def action_toggle_enabled(self) -> None:
        aid = self._selected_account_id()
        if aid is None:
            self.notify("Select an account row first")
            return
        enabled = self.registry.toggle(aid)
        save_registry(self.registry)
        name = self._account_name(aid)
        short = name if len(name) <= 40 else name[:37] + "..."
        self.notify(f"{short} {'enabled' if enabled else 'disabled'}")
        self._refresh_table()

    @work(thread=True)
    def _run(self, execute: bool) -> None:
        if not self.broker:
            return
        try:
            drive = self._drive()
            src = "stream 09:30-09:45" if self.stream else "unknown"
            confirm = not self.registry.settings.confirm_nukes or execute
            if execute and self.registry.settings.confirm_nukes:
                # caller must use execute action which checks nukes separately
                confirm = True
            summary = run_session(
                self.broker, drive=drive, drive_source=src, registry=self.registry,
                execute=execute,
                confirm_nukes=execute,  # execute path always confirms when user hits X
            )
            n = len(summary.results)
            if n == 0:
                msg = (
                    f"{'Executed' if execute else 'Dry-run'}: 0 accounts — "
                    "enable tradeable rows (E) or check drive signal"
                )
            else:
                msg = (
                    f"{'Executed' if execute else 'Dry-run'}: {n} accts, "
                    f"{summary.orders_placed} orders placed"
                )
                if summary.orders_placed == 0 and execute:
                    msg += " (drive may be flat / on hold, or plans are HOLD)"
            self.call_from_thread(self.notify, msg)
            self.call_from_thread(self._refresh_table)
        except ProjectXError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")

    def action_dry_run(self) -> None:
        self._run(execute=False)

    def action_execute(self) -> None:
        if self.registry.settings.confirm_nukes:
            self.notify("Executing (including nukes) — press X only when ready", severity="warning")
        self._run(execute=True)

    def on_unmount(self) -> None:
        if self.stream:
            self.stream.stop()
        if self.broker:
            self.broker.close()


def run_dashboard() -> None:
    load_dotenv()
    # Same single-instance guard as the web server: the TUI also logs in with the
    # shared API key and writes the shared state files, so it must not run beside
    # another TopHat instance.
    from tophat.store import single_instance
    try:
        lock = single_instance.acquire_or_none()
    except single_instance.AlreadyRunning as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
    try:
        TopHatApp().run()
    finally:
        if lock is not None:
            lock.release()
