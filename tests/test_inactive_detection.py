"""Automatic inactive detection: balance at/below the trailing MLL floor marks
an account inactive (status + plan) and blocks firing, regardless of the
broker's canTrade flag."""

from datetime import datetime
from zoneinfo import ZoneInfo

from tophat.broker.base import BrokerAccount
from tophat.broker.mock import MockBroker
from tophat.server import service
from tophat.store import registry as R

ET = ZoneInfo("America/New_York")
DAY0 = datetime(2026, 6, 22, 9, 46, tzinfo=ET)


def _only_row(broker, key="mll"):
    snap = service.build_snapshot(broker, mode="mock", key=key, owner=key)
    return snap["accounts"][0]


def test_balance_below_mll_marks_inactive():
    # Fresh eval: floor = 50,000 - 2,000 = 48,000. Broker still says canTrade=true,
    # but a 47,500 balance is under the MLL -> auto-inactive, idle plan.
    b = MockBroker(n_eval=1, n_funded=0, seed=61)
    acct = b.list_accounts()[0]
    b._accounts[0] = BrokerAccount(acct.account_id, acct.name, 47_500.0, True, True)
    row = _only_row(b)
    assert row["status"] == "inactive"
    assert row["can_trade"] is False
    assert row["broker_can_trade"] is True
    assert row["plan_action"] == "idle"
    assert "MLL" in row["plan_note"]


def test_balance_above_mll_stays_active():
    b = MockBroker(n_eval=1, n_funded=0, seed=62)
    acct = b.list_accounts()[0]
    b._accounts[0] = BrokerAccount(acct.account_id, acct.name, 48_500.0, True, True)
    row = _only_row(b, key="mll2")
    assert row["status"] == "active"
    assert row["can_trade"] is True


def test_run_session_never_fires_below_mll(ctl):
    # Funded fresh floor = min(0, peak - 2,000) = -2,000; a -2,500 balance is dead.
    ctl.set_balance(-2_500.0)
    reg = R.load_registry()
    reg.entry(ctl.aid).enabled = True
    R.save_registry(reg)
    r = service.run_session(ctl, execute=True, manual=True, now_et=DAY0)
    row = next(x for x in r["results"] if x["account_id"] == ctl.aid)
    assert row["action"] == "idle"
    assert "MLL" in row["note"]
    assert r["orders_placed"] == 0


def test_exclude_analytics_roundtrip(client):
    aid = client.get("/api/accounts").json()[0]["account_id"]
    client.post(f"/api/accounts/{aid}/lifecycle", json={"exclude_analytics": True})
    row = next(a for a in client.get("/api/accounts").json()
               if a["account_id"] == aid)
    assert row["exclude_analytics"] is True
