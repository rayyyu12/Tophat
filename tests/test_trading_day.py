"""Trading-day rollover: nuke slots and the drive reset at the ~4 PM CT session
boundary, not midnight ET.

Reproduces the 2026-07-09 report — a funded nuke-cycle account stuck "waiting
for a nuke slot" late at night (the prior session's stamp still held the single
daily slot) and the drive still showing LONG at 11 PM (this morning's opening
range served all evening). Both keyed off the ET calendar date, which rolls at
midnight; keying off `trading_day` makes them turn over when the session does.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.broker.base import BrokerAccount
from tophat.broker.mock import MockBroker
from tophat.clock import trading_day
from tophat.engine import AccountState, Phase
from tophat.server import service
from tophat.services.scheduler import ScheduleState, save_schedule
from tophat.store.registry import load_registry, save_registry
from tophat.store.states import save_all

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")


def _fake_now(monkeypatch, dt: datetime) -> None:
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.astimezone(tz) if tz else dt

    monkeypatch.setattr(service, "datetime", FakeDT)


# --- trading_day boundary ------------------------------------------------------

def test_trading_day_rolls_at_4pm_ct():
    # 09:45 ET (08:45 CT) morning entry — the calendar date, always.
    assert trading_day(datetime(2026, 7, 9, 9, 45, tzinfo=ET)) == "2026-07-09"
    # 15:59 CT — still the same trading day.
    assert trading_day(datetime(2026, 7, 9, 15, 59, tzinfo=CT)) == "2026-07-09"
    # 16:00 CT sharp — rolls to the next trading day.
    assert trading_day(datetime(2026, 7, 9, 16, 0, tzinfo=CT)) == "2026-07-10"
    # 23:00 ET (22:00 CT) — the operator's "11 PM still shows yesterday" window.
    assert trading_day(datetime(2026, 7, 9, 23, 0, tzinfo=ET)) == "2026-07-10"
    # Overnight before the next open — the next trading day (RTH not yet formed).
    assert trading_day(datetime(2026, 7, 10, 2, 0, tzinfo=ET)) == "2026-07-10"


def test_trading_day_custom_reset():
    assert trading_day(datetime(2026, 7, 9, 16, 30, tzinfo=CT), "17:00") == "2026-07-09"
    assert trading_day(datetime(2026, 7, 9, 17, 0, tzinfo=CT), "17:00") == "2026-07-10"


# --- drive resets at the session rollover, not midnight ------------------------

def test_drive_flat_after_session_rollover(monkeypatch):
    b = MockBroker(n_eval=1, n_funded=0)
    service._DRIVE_CACHE.update(date="", value=None, src="")
    try:
        # In-session (10:00 ET): the mock drives LONG and it locks for the day.
        _fake_now(monkeypatch, datetime(2026, 7, 9, 10, 0, tzinfo=ET))
        assert service._drive(b, "NQ") == (1, "stream/bars")
        assert service._DRIVE_CACHE["date"] == "2026-07-09"
        # Same evening, past the 4 PM CT rollover: flat — the stale morning cache
        # is NOT re-served (and never re-cached under the new trading day).
        _fake_now(monkeypatch, datetime(2026, 7, 9, 23, 0, tzinfo=ET))
        val, src = service._drive(b, "NQ")
        assert val == 0 and "session closed" in src
        assert service._DRIVE_CACHE["date"] == "2026-07-09"   # untouched
    finally:
        service._DRIVE_CACHE.update(date="", value=None, src="")


def test_drive_public_flat_after_rollover(monkeypatch):
    service._DRIVE_CACHE.update(date="2026-07-09", value=1, src="stream/bars")
    try:
        _fake_now(monkeypatch, datetime(2026, 7, 9, 10, 0, tzinfo=ET))
        assert service.drive_public()["drive"] == "LONG"      # in-session: locked
        _fake_now(monkeypatch, datetime(2026, 7, 9, 23, 0, tzinfo=ET))
        assert service.drive_public()["locked"] is False      # evening: flat
    finally:
        service._DRIVE_CACHE.update(date="", value=None, src="")


# --- the nuke slot frees at the rollover (the reported bug) --------------------

def _funded(**kw) -> AccountState:
    st = AccountState()
    st.phase = Phase.FUNDED
    st.base_balance = 0.0
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_nuke_slot_frees_after_rollover(monkeypatch):
    b = MockBroker(n_eval=0, n_funded=2, seed=1)
    wait_id = b._accounts[0].account_id
    ghost_id = b._accounts[1].account_id
    # Positive balances so both clear the trailing-MLL floor and stay tradeable.
    b._accounts[0] = BrokerAccount(wait_id, "EXPRESS-XFA-1", 3000.0, True, True)
    b._accounts[1] = BrokerAccount(ghost_id, "EXPRESS-XFA-2", 3000.0, True, True)
    # wait_id: funded, nuke-cycle, not yet nuked -> a live nuke candidate.
    # ghost_id: funded but already nuked this cycle (now a flip) AND holds today's
    # nuke stamp -> consumes the single daily slot as a ghost until the rollover.
    save_all({
        wait_id: _funded(payouts_taken=0, nuke_hit_this_cycle=False, equity=3000.0),
        ghost_id: _funded(payouts_taken=0, nuke_hit_this_cycle=True, equity=3000.0),
    })
    reg = load_registry()
    reg.entry(wait_id).enabled = True
    reg.entry(ghost_id).enabled = True
    save_registry(reg)
    save_schedule(ScheduleState(last_nuke_date={ghost_id: "2026-07-09"},
                                slot_owner={ghost_id: "o"}))

    def wait_plan(now: datetime):
        _fake_now(monkeypatch, now)
        service.invalidate_snapshot_cache()
        snap = service.build_snapshot(b, mode="mock", key="o", owner="o")
        row = next(r for r in snap["accounts"] if r["account_id"] == wait_id)
        return row["plan_action"], row["plan_note"]

    # Morning of the stamp day: the ghost still holds the slot -> waiting.
    action, note = wait_plan(datetime(2026, 7, 9, 10, 0, tzinfo=ET))
    assert action == "idle" and "waiting for a nuke slot" in note
    # Same evening, past the 4 PM CT rollover: last session's stamp is stale, so
    # the slot is free and the candidate takes it for the next session.
    action, _ = wait_plan(datetime(2026, 7, 9, 23, 0, tzinfo=ET))
    assert action == "nuke"
