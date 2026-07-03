"""Fire-timing + drive-direction correctness (the 09:45:00 entry spec).

- ProjectX retrieveBars returns bars NEWEST-first; the drive read must sort them
  or the direction comes out inverted (compares 09:30's close to 09:44's open).
- The automation loop must wake AT entry times, not up to an interval later.
- Entries have a bounded grace window: a slot that comes due late (freed by a
  mid-morning eval pass / account enabled at 10:30) must not fire off-strategy.
- The forming (pre-09:45) drive must never be cached as the day's direction.
- Practice accounts never trade and never consume an eval slot.
- merge_save must not clobber entries another writer persisted after our load.
"""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tophat.broker.base import BrokerAccount
from tophat.broker.mock import MockBroker
from tophat.engine import AccountState
from tophat.server import service
from tophat.server.service import BrokerHandle
from tophat.services.automation import Automation, _entry_times
from tophat.store.config import load_settings, save_settings, update_settings
from tophat.store.states import load_all, merge_save, save_all

ET = ZoneInfo("America/New_York")


def _arm(**kw):
    s = load_settings()
    s.auto_execute = True
    for k, v in kw.items():
        setattr(s, k, v)
    save_settings(s)


# --- drive read: bar ordering ------------------------------------------------

def _fake_now(monkeypatch, dt: datetime) -> None:
    from tophat.broker.projectx import client as client_mod

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.astimezone(tz) if tz else dt

    monkeypatch.setattr(client_mod, "datetime", FakeDT)


def _client_with_bars(bars):
    from tophat.broker.projectx.client import ProjectXClient
    c = ProjectXClient("u", "k")
    c.retrieve_bars = lambda *a, **k: bars
    return c


def test_drive_read_correct_with_newest_first_bars(monkeypatch):
    # True session: opened 100.0 at 09:30, closed 110.0 at 09:44 -> LONG.
    # Served newest-first (as ProjectX returns them); unsorted this read -1.
    _fake_now(monkeypatch, datetime(2026, 7, 1, 10, 0, tzinfo=ET))
    bars = [
        {"t": "2026-07-01T13:44:00+00:00", "o": 109.0, "c": 110.0},
        {"t": "2026-07-01T13:35:00+00:00", "o": 104.0, "c": 105.0},
        {"t": "2026-07-01T13:30:00+00:00", "o": 100.0, "c": 101.0},
    ]
    assert _client_with_bars(bars).drive_direction_from_bars("NQ") == 1


def test_drive_read_short_with_newest_first_bars(monkeypatch):
    _fake_now(monkeypatch, datetime(2026, 7, 1, 10, 0, tzinfo=ET))
    bars = [
        {"t": "2026-07-01T13:44:00+00:00", "o": 91.0, "c": 90.0},
        {"t": "2026-07-01T13:30:00+00:00", "o": 100.0, "c": 99.0},
    ]
    assert _client_with_bars(bars).drive_direction_from_bars("NQ") == -1


# --- drive cache: never lock the forming range ---------------------------------

def test_forming_drive_is_not_cached(monkeypatch):
    fixed = {"now": datetime(2026, 1, 2, 9, 31, tzinfo=ET)}  # Friday, pre-lock

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed["now"].astimezone(tz) if tz else fixed["now"]

    monkeypatch.setattr(service, "datetime", FakeDT)
    service._DRIVE_CACHE.update(date="", value=None, src="")
    try:
        b = MockBroker(n_eval=1, n_funded=0)
        val, src = service._drive(b, "NQ")
        assert val == 1 and src.startswith("forming")
        assert service._DRIVE_CACHE["value"] is None      # nothing locked yet
        fixed["now"] = datetime(2026, 1, 2, 9, 45, 0, 500000, tzinfo=ET)
        val, src = service._drive(b, "NQ")
        assert (val, src) == (1, "stream/bars")
        assert service._DRIVE_CACHE["date"] == "2026-01-02"  # locked at 09:45
    finally:
        service._DRIVE_CACHE.update(date="", value=None, src="")


# --- automation wake-up precision ----------------------------------------------

def test_next_entry_delay_lands_on_the_entry_second():
    s = SimpleNamespace(flip_stagger_times=["09:45", "10:00"], nuke_entry_time="09:45")
    a = Automation(lambda: [])
    now = datetime(2026, 7, 1, 9, 44, 30, tzinfo=ET)          # Wednesday
    assert a._next_entry_delay(now, s) == 30.0
    assert a._next_entry_delay(datetime(2026, 7, 1, 9, 59, 59, tzinfo=ET), s) == 1.0
    assert a._next_entry_delay(datetime(2026, 7, 1, 10, 1, 0, tzinfo=ET), s) is None


def test_window_opens_early_for_warmup_and_skips_weekends():
    s = SimpleNamespace(flip_stagger_times=["10:00"], nuke_entry_time="09:45")
    a = Automation(lambda: [])
    assert a._in_window(datetime(2026, 7, 1, 9, 43, 0, tzinfo=ET), s)       # prep tick
    assert not a._in_window(datetime(2026, 7, 1, 9, 42, 59, tzinfo=ET), s)
    assert a._in_window(datetime(2026, 7, 1, 11, 30, 0, tzinfo=ET), s)
    assert not a._in_window(datetime(2026, 7, 1, 11, 31, 0, tzinfo=ET), s)
    assert not a._in_window(datetime(2026, 7, 4, 10, 0, 0, tzinfo=ET), s)   # Saturday
    junk = SimpleNamespace(flip_stagger_times=["junk"], nuke_entry_time="bad")
    assert _entry_times(junk) == [(9, 45)]                    # malformed -> default


# --- entry grace window ---------------------------------------------------------

def test_late_slot_does_not_fire_off_strategy():
    _arm()
    b = MockBroker(n_eval=1, n_funded=0, seed=3)
    pool = [BrokerHandle("o", b, "mock")]
    late = datetime(2026, 7, 1, 10, 30, tzinfo=ET)   # 09:45 entry + 10m grace long gone
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=late)
    assert out["orders_placed"] == 0
    assert any(r["action"] == "missed" for r in out["results"])
    # ...but AT the entry second it fires normally.
    on_time = datetime(2026, 7, 1, 9, 45, 0, 200000, tzinfo=ET)
    out = service.run_all_sessions(pool, execute=True, respect_times=True, now_et=on_time)
    assert out["orders_placed"] == 1


# --- practice accounts ----------------------------------------------------------

def test_practice_account_never_fires_or_takes_the_eval_slot():
    _arm(max_evals_per_day=1)
    b = MockBroker(n_eval=2, n_funded=0, seed=4)
    prac_id = b._accounts[0].account_id
    real_id = b._accounts[1].account_id
    # Lowest id + never-traded: without the guard this would win the only slot.
    b._accounts[0] = BrokerAccount(prac_id, "PRAC-V2-1", 150_000.0, True, True)
    out = service.run_all_sessions([BrokerHandle("o", b, "mock")], execute=True)
    assert out["orders_placed"] == 1
    fired = {r["account_id"] for r in out["results"] if r.get("order_id")}
    assert fired == {real_id}


def test_practice_account_fires_on_explicit_manual_execute():
    # Validation path: the operator enables a practice account and fires by hand.
    _arm(max_evals_per_day=1)
    b = MockBroker(n_eval=1, n_funded=0, seed=8)
    prac_id = b._accounts[0].account_id
    b._accounts[0] = BrokerAccount(prac_id, "PRAC-V2-9", 150_000.0, True, True)
    out = service.run_all_sessions([BrokerHandle("o", b, "mock")],
                                   execute=True, manual=True)
    fired = {r["account_id"] for r in out["results"] if r.get("order_id")}
    assert fired == {prac_id}


# --- concurrent-writer safety ----------------------------------------------------

def test_merge_save_preserves_another_writers_fire():
    save_all({1: AccountState(last_fire_date="2026-07-01", pending_label="eval")})
    # A stale writer (loaded before the fire) persists only the account it changed.
    stale = {1: AccountState(), 2: AccountState(days_traded=3)}
    merge_save(stale, [2])
    disk = load_all()
    assert disk[1].last_fire_date == "2026-07-01"    # the recorded fire survives
    assert disk[1].pending_label == "eval"
    assert disk[2].days_traded == 3


# --- settings validation ----------------------------------------------------------

def test_entry_times_normalized_and_validated():
    s = update_settings({"nuke_entry_time": "9:45", "flip_stagger_times": ["9:45", "10:00"]})
    assert s.nuke_entry_time == "09:45"
    assert s.flip_stagger_times == ["09:45", "10:00"]
    with pytest.raises(ValueError):
        update_settings({"nuke_entry_time": "quarter to ten"})
