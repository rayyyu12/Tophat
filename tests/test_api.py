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
    assert {"account_id", "phase", "balance", "plan_action", "payout_ready"} <= set(row)


def test_funded_accounts_inferred_from_balance(client):
    # mock funded accounts start near $0 -> must be phase 'funded', not 'eval'
    s = client.get("/api/state").json()
    funded = [a for a in s["accounts"] if a["balance"] < 25_000]
    assert funded and all(a["phase"] in ("funded", "blown", "retired") for a in funded)


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


def test_automation_status(client):
    a = client.get("/api/automation").json()
    assert a["running"] is True and a["auto_execute"] is False  # safe default
