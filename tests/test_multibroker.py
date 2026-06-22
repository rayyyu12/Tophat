"""Per-credential independence: each owner runs its own daily rules."""

from tophat.broker.mock import MockBroker
from tophat.server import service
from tophat.server.service import BrokerHandle
from tophat.store.config import load_settings, save_settings


def test_each_owner_nukes_independently():
    """Two owners, one funded account each, global cap of 1 nuke/day.

    Because each broker's plan is assigned over only its own accounts, BOTH owners
    nuke on the same day — the cap is per user, not per fleet.
    """
    s = load_settings()
    s.auto_execute = True
    s.max_nukes_per_day = 1
    save_settings(s)

    b1 = MockBroker(n_eval=0, n_funded=1, seed=11)
    b2 = MockBroker(n_eval=0, n_funded=1, seed=12)
    pool = [BrokerHandle("alice", b1, "mock"), BrokerHandle("bob", b2, "mock")]

    out = service.run_all_sessions(pool, execute=True)

    nukes = [r for r in out["results"] if r["action"] in ("nuke", "renuke")]
    assert out["orders_placed"] == 2
    assert {r["owner"] for r in nukes} == {"alice", "bob"}


def test_dashboard_groups_one_table_per_owner():
    b1 = MockBroker(n_eval=1, n_funded=1, seed=21)
    b2 = MockBroker(n_eval=2, n_funded=0, seed=22)
    pool = [BrokerHandle("alice", b1, "mock"), BrokerHandle("bob", b2, "mock")]

    dash = service.build_dashboard(pool)

    owners = [g["owner"] for g in dash["groups"]]
    assert owners == ["alice", "bob"]
    # flat union is the sum of the per-owner tables, and every row is stamped owner
    assert len(dash["accounts"]) == sum(len(g["accounts"]) for g in dash["groups"])
    assert all(r["owner"] in ("alice", "bob") for r in dash["accounts"])
    assert dash["counts"]["total"] == len(dash["accounts"])


def test_unreadable_broker_becomes_error_group_not_crash():
    pool = [BrokerHandle("ghost", None, "live", error="login failed")]
    dash = service.build_dashboard(pool)
    assert dash["groups"][0]["error"] == "login failed"
    assert dash["accounts"] == []
