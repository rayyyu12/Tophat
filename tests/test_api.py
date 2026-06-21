"""Integration tests for the FastAPI app (auth gate + endpoints, mock broker)."""

from fastapi.testclient import TestClient

from tophat.server import auth
from tophat.server.app import create_app


def test_auth_gate_blocks_unauthenticated():
    auth.create_user("t@x.com", "pw123")
    with TestClient(create_app()) as c:
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/automation").status_code == 401
        assert c.get("/", follow_redirects=False).status_code in (302, 307)
        assert c.get("/login").status_code == 200          # public


def test_login_bad_then_good():
    auth.create_user("t@x.com", "pw123")
    with TestClient(create_app()) as c:
        assert c.post("/api/login", json={"email": "t@x.com", "password": "no"}).status_code == 401
        r = c.post("/api/login", json={"email": "t@x.com", "password": "pw123"})
        assert r.status_code == 200
        assert c.get("/api/state").status_code == 200      # cookie now set


def test_logout_clears_session():
    auth.create_user("t@x.com", "pw123")
    with TestClient(create_app()) as c:
        c.post("/api/login", json={"email": "t@x.com", "password": "pw123"})
        assert c.get("/api/state").status_code == 200
        assert c.post("/api/logout").status_code == 200
        assert c.get("/api/state").status_code == 401


def test_state_shape(client):
    s = client.get("/api/state").json()
    assert s["mode"] == "mock"
    assert {"total", "funded", "eval"} <= set(s["counts"])
    assert isinstance(s["accounts"], list) and s["accounts"]
    row = s["accounts"][0]
    assert {"account_id", "phase", "program", "status", "terminal", "balance",
            "plan_action", "payout_ready", "can_trade", "trading_status"} <= set(row)


def test_list_accounts_detail(client):
    rows = client.get("/api/accounts").json()
    assert rows and "state" in rows[0] and "lifecycle" in rows[0]


def test_lifecycle_patch(client):
    rows = client.get("/api/accounts").json()
    aid = rows[0]["account_id"]
    r = client.post(f"/api/accounts/{aid}/lifecycle", json={
        "payouts_taken": 1, "winning_days_this_cycle": 3,
        "nuke_hit_this_cycle": True, "nuke_tries_this_cycle": 1,
    }).json()
    assert r["ok"] is True
    assert r["state"]["payouts_taken"] == 1
    assert r["state"]["winning_days_this_cycle"] == 3


def test_force_inactive_overrides_broker(client):
    rows = client.get("/api/accounts").json()
    aid = rows[0]["account_id"]
    client.post(f"/api/accounts/{aid}/lifecycle", json={"force_inactive": True})
    row = next(a for a in client.get("/api/state").json()["accounts"] if a["account_id"] == aid)
    assert row["trading_status"] == "inactive"
    assert row["broker_can_trade"] is True
    assert row["force_inactive"] is True


def test_funded_accounts_inferred_from_name(client):
    s = client.get("/api/state").json()
    for a in s["accounts"]:
        name = a["name"].upper()
        if "EXPRESS" in name:
            assert a["phase"] == "funded"
        elif "50KTC" in name:
            assert a["phase"] == "eval"


def test_settings_roundtrip(client):
    base = client.get("/api/settings").json()
    assert base["nuke_target_dollars"] == 3_200.0
    client.post("/api/settings", json={"nuke_target_dollars": 3_000.0})
    assert client.get("/api/settings").json()["nuke_target_dollars"] == 3_000.0


def test_run_preview_places_nothing(client):
    r = client.post("/api/run", json={"execute": False}).json()
    assert r["executed"] is False and r["orders_placed"] == 0


def test_toggle_account(client):
    aid = client.get("/api/state").json()["accounts"][0]["account_id"]
    out = client.post(f"/api/accounts/{aid}/toggle").json()
    assert out["enabled"] is False


def test_set_enabled_is_idempotent(client):
    """Idempotent set (not flip) so rapid repeated clicks converge, never race."""
    aid = client.get("/api/state").json()["accounts"][0]["account_id"]
    assert client.post(f"/api/accounts/{aid}/set-enabled", json={"enabled": False}).json()["enabled"] is False
    assert client.post(f"/api/accounts/{aid}/set-enabled", json={"enabled": False}).json()["enabled"] is False
    assert client.post(f"/api/accounts/{aid}/set-enabled", json={"enabled": True}).json()["enabled"] is True
    assert client.post(f"/api/accounts/{aid}/set-enabled", json={"enabled": True}).json()["enabled"] is True


def test_automation_status(client):
    a = client.get("/api/automation").json()
    assert a["running"] is True and a["auto_execute"] is False  # safe default


def test_status_decoupled_from_phase(client):
    """A blown account: Status carries 'blown', Phase stays the program type."""
    rows = client.get("/api/accounts").json()
    aid = next(a["account_id"] for a in rows if a["phase"] == "funded")
    client.post(f"/api/accounts/{aid}/lifecycle", json={"phase": "blown"})
    row = next(a for a in client.get("/api/state").json()["accounts"]
               if a["account_id"] == aid)
    assert row["status"] == "blown"          # operational state
    assert row["program"] == "funded"        # program type unchanged
    assert row["terminal"] is True
    assert row["plan_action"] != "flip"      # no flip/nuke plan for terminal


def test_payout_ready_shows_payout_not_flip(client):
    """A payout-ready funded account must read 'payout_ready', never 'flip slot N'."""
    rows = client.get("/api/accounts").json()
    aid = next(a["account_id"] for a in rows if a["phase"] == "funded")
    client.post(f"/api/accounts/{aid}/lifecycle", json={
        "payouts_taken": 2, "winning_days_this_cycle": 5,
        "nuke_hit_this_cycle": True, "nuke_tries_this_cycle": 1,
        "payout_ready": True})
    row = next(a for a in client.get("/api/state").json()["accounts"]
               if a["account_id"] == aid)
    assert row["payout_ready"] is True
    assert row["plan_action"] == "payout_ready"
    assert row["plan_preview"]["plan_action"] == "payout_ready"


def test_manual_execute_fires_while_disarmed(client):
    """Manual Execute (manual=True) fires even though auto_execute is off;
    the scheduler path (manual omitted) does not."""
    scheduler = client.post("/api/run", json={"execute": True}).json()
    assert scheduler["executed"] is False           # gated by auto_execute
    manual = client.post("/api/run", json={"execute": True, "manual": True}).json()
    assert manual["executed"] is True               # explicit operator action


def test_snapshot_cache_hits_and_invalidates(monkeypatch):
    """With a positive TTL, build_snapshot serves a cached object until invalidated."""
    from tophat.broker.mock import MockBroker
    from tophat.server import service
    monkeypatch.setattr(service, "SNAPSHOT_TTL", 999.0)
    service.invalidate_snapshot_cache()
    b = MockBroker(seed=3)
    first = service.build_snapshot(b, mode="mock")
    assert service.build_snapshot(b, mode="mock") is first        # cache hit
    service.invalidate_snapshot_cache()
    assert service.build_snapshot(b, mode="mock") is not first    # rebuilt
    service.invalidate_snapshot_cache()
