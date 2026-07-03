"""Snapshot status/plan precedence + no redundant broker polling."""

from tophat.broker.mock import MockBroker
from tophat.server import service
from tophat.server.service import BrokerHandle
from tophat.store.registry import load_registry, save_registry


def _only_row(broker):
    snap = service.build_snapshot(broker, mode="mock", key="o", owner="o")
    return snap["accounts"][0]


def _set_entry(aid, *, enabled, force_inactive):
    reg = load_registry()
    e = reg.entry(aid)
    e.enabled = enabled
    e.force_inactive = force_inactive
    save_registry(reg)
    service.invalidate_snapshot_cache()


def test_disabled_and_inactive_reads_disabled_not_idle():
    # The reported bug: a disabled + inactive account showed "Idle" (and rendered
    # bright) instead of "Disabled". Disabled must outrank inactive in the plan cell.
    b = MockBroker(n_eval=1, n_funded=0, seed=31)
    aid = b.list_accounts()[0].account_id
    _set_entry(aid, enabled=False, force_inactive=True)
    row = _only_row(b)
    assert row["plan_action"] == "disabled"
    assert row["can_trade"] is False      # genuinely inactive
    assert row["enabled"] is False


def test_enabled_but_inactive_still_reads_idle():
    b = MockBroker(n_eval=1, n_funded=0, seed=32)
    aid = b.list_accounts()[0].account_id
    _set_entry(aid, enabled=True, force_inactive=True)
    row = _only_row(b)
    assert row["plan_action"] == "idle"
    assert "inactive" in row["plan_note"]


def test_list_accounts_detail_does_not_double_fetch_accounts():
    # Regression: list_accounts_detail used to re-call list_accounts() per broker
    # after the cached snapshot already had the rows — a second /api/Account/search
    # on every Accounts-page load (429 risk).
    class Counting(MockBroker):
        calls = 0
        def list_accounts(self, **kw):
            Counting.calls += 1
            return super().list_accounts(**kw)

    Counting.calls = 0
    b = Counting(n_eval=2, n_funded=1, seed=41)
    service.invalidate_snapshot_cache()
    service.list_accounts_detail([BrokerHandle("o", b, "mock")])
    assert Counting.calls == 1            # only the snapshot build, no second pass
