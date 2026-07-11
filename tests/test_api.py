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


def test_webhook_setting_roundtrip_and_validation(client):
    url = "https://discord.com/api/webhooks/123/abc"
    r = client.post("/api/settings", json={"discord_webhook_url": url})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["discord_webhook_url"] == url
    # junk paste is rejected and the stored value survives
    r = client.post("/api/settings", json={"discord_webhook_url": "http://evil.example/x"})
    assert r.status_code == 400
    assert client.get("/api/settings").json()["discord_webhook_url"] == url
    # clearing turns notifications off
    r = client.post("/api/settings", json={"discord_webhook_url": ""})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["discord_webhook_url"] == ""


def test_webhook_test_endpoint_requires_a_url(client):
    r = client.post("/api/settings/test-webhook", json={})
    assert r.status_code == 400 and "no webhook" in r.json()["error"]


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


def test_credentials_crud_and_masking(client, monkeypatch):
    """Add/reveal/delete keys; list responses are masked, reveal returns the raw key."""
    from tophat.broker.mock import MockBroker
    from tophat.server import service
    # Adding a key rebuilds the broker pool; keep that hermetic (no live login).
    monkeypatch.setattr(service, "build_broker_pool",
                        lambda: [service.BrokerHandle("alice", MockBroker(), "mock")])

    assert client.get("/api/credentials").json() == []

    out = client.post("/api/credentials",
                      json={"username": "alice", "api_key": "SECRET-KEY-1234"}).json()
    assert out[0]["username"] == "alice"
    assert "api_key" not in out[0]                  # never the raw key in the list
    assert set(out[0]["masked"]) <= {"•"}           # fully masked, no raw chars

    rev = client.post("/api/credentials/alice/reveal").json()
    assert rev["api_key"] == "SECRET-KEY-1234"      # reveal is the only raw path

    assert client.delete("/api/credentials/alice").json() == []


def test_credentials_validation(client):
    r = client.post("/api/credentials", json={"username": "x", "api_key": ""})
    assert r.status_code == 400


def test_state_rows_carry_owner(client):
    s = client.get("/api/state").json()
    assert "groups" in s and s["groups"]
    assert all("owner" in a for a in s["accounts"])


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


# ---- payouts_taken override propagation (accounts tab + mirror update) ----

def _funded_aid(client):
    rows = client.get("/api/accounts").json()
    return next(a["account_id"] for a in rows if a["phase"] == "funded")


def test_override_payout_increment_records_everywhere(client):
    """Raising 'Payouts taken' with record_payouts must behave exactly like the
    Mark-payout button: ledger event (-> Analytics), cycle reset, re-enable."""
    from tophat.store import trade_log
    aid = _funded_aid(client)
    client.post(f"/api/accounts/{aid}/lifecycle", json={
        "phase": "funded", "base_balance": 0, "equity": 4000.0,
        "winning_days_this_cycle": 5, "payout_ready": True})
    client.post(f"/api/accounts/{aid}/set-enabled", json={"enabled": False})
    # simulate the edit form: full field set posted alongside the raise
    r = client.post(f"/api/accounts/{aid}/lifecycle", json={
        "payouts_taken": 1, "record_payouts": True, "phase": "funded",
        "winning_days_this_cycle": 5, "payout_ready": True,
        "equity": 4000.0}).json()
    assert r["payouts_recorded"] == 1
    assert r["state"]["payouts_taken"] == 1
    assert r["state"]["winning_days_this_cycle"] == 0   # cycle reset won
    assert r["state"]["payout_ready"] is False
    assert r["enabled"] is True                          # re-enabled for next cycle
    evts = [e for e in trade_log.read_events()
            if e.get("type") == "payout" and e.get("account_id") == aid]
    assert len(evts) == 1
    assert evts[0]["amount"] == 2000.0                   # min(cap, 4000/2)
    assert evts[0]["via"] == "override"
    banked = client.get("/api/analytics").json()["totals"]["payouts_banked"]
    assert banked == 2000.0


def test_override_payout_decrement_reverses_ledger(client):
    from tophat.store import trade_log
    aid = _funded_aid(client)
    client.post(f"/api/accounts/{aid}/lifecycle", json={
        "phase": "funded", "base_balance": 0, "equity": 4000.0})
    client.post(f"/api/accounts/{aid}/lifecycle", json={
        "payouts_taken": 1, "record_payouts": True})
    r = client.post(f"/api/accounts/{aid}/lifecycle", json={
        "payouts_taken": 0}).json()
    assert r["payouts_reversed"] == 1
    assert r["state"]["payouts_taken"] == 0
    amounts = [e["amount"] for e in trade_log.read_events()
               if e.get("type") == "payout" and e.get("account_id") == aid]
    assert sorted(amounts) == [-2000.0, 2000.0]
    assert client.get("/api/analytics").json()["totals"]["payouts_banked"] == 0.0


def test_override_without_flag_adopts_history_silently(client):
    """Presets / adopting an account mid-lifecycle: raw counter set, no events."""
    from tophat.store import trade_log
    aid = _funded_aid(client)
    r = client.post(f"/api/accounts/{aid}/lifecycle", json={
        "payouts_taken": 2, "winning_days_this_cycle": 3}).json()
    assert r["payouts_recorded"] == 0
    assert r["state"]["payouts_taken"] == 2
    assert r["state"]["winning_days_this_cycle"] == 3    # NOT reset
    assert not [e for e in trade_log.read_events() if e.get("type") == "payout"]


def test_override_payout_retires_at_target(client):
    aid = _funded_aid(client)
    client.post(f"/api/accounts/{aid}/lifecycle", json={
        "phase": "funded", "base_balance": 0, "equity": 1000.0})
    r = client.post(f"/api/accounts/{aid}/lifecycle", json={
        "payouts_taken": 4, "record_payouts": True}).json()
    assert r["payouts_recorded"] == 4
    assert r["state"]["phase"] == "retired"


def test_mirror_override_payout_full_semantics(client):
    """Mirror payouts_taken raise with record_payouts == the Mark Paid button:
    equity drop, ledger event, window reset, apex channel -> nuke."""
    from tophat.store import trade_log
    m = client.post("/api/mirrors", json={
        "firm": "apex-50k", "account_number": "PA123", "phase": "funded"}).json()
    mid = m["mirror_id"]
    client.post(f"/api/mirrors/{mid}/update", json={
        "equity": 3000.0, "win_days": 5, "channel": "flip"})
    r = client.post(f"/api/mirrors/{mid}/update", json={
        "payouts_taken": 1, "record_payouts": True}).json()
    assert r["payouts_taken"] == 1
    assert r["equity"] == 1500.0          # 3000 - min(request 1500, cap, equity)
    assert r["win_days"] == 0
    assert r["channel"] == "nuke"         # apex-gate cycle reopens on nuke
    evts = [e for e in trade_log.read_events()
            if e.get("type") == "payout" and e.get("mirror_id") == mid]
    assert len(evts) == 1 and evts[0]["amount"] == 1500.0
    # decrement reverses the ledger event
    r2 = client.post(f"/api/mirrors/{mid}/update", json={"payouts_taken": 0}).json()
    assert r2["payouts_taken"] == 0
    amounts = [e["amount"] for e in trade_log.read_events()
               if e.get("type") == "payout" and e.get("mirror_id") == mid]
    assert sorted(amounts) == [-1500.0, 1500.0]


def test_mirror_override_without_flag_is_raw(client):
    from tophat.store import trade_log
    m = client.post("/api/mirrors", json={
        "firm": "lucid-50k", "account_number": "L1", "phase": "funded"}).json()
    r = client.post(f"/api/mirrors/{m['mirror_id']}/update",
                    json={"payouts_taken": 2}).json()
    assert r["payouts_taken"] == 2
    assert not [e for e in trade_log.read_events()
                if e.get("type") == "payout" and e.get("mirror_id") == m["mirror_id"]]
