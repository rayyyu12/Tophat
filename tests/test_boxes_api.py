"""Copier-box pairing + the token-authed Rabbit endpoints (/api/ops/tc-*)."""

import json

from tophat.store import tenant
from tophat.store.paths import TC_STATUS_FILE


def _mint(client, name="test box"):
    r = client.post("/api/boxes", json={"name": name})
    assert r.status_code == 200
    return r.json()


def test_create_list_and_token_shown_once(client):
    d = _mint(client)
    assert d["token"].startswith("thr_") and len(d["token"]) > 40
    listing = client.get("/api/boxes").json()["boxes"]
    assert len(listing) == 1
    row = listing[0]
    assert row["name"] == "test box"
    assert "token" not in row and row["hint"] == d["token"][:8] + "…"


def test_tc_desired_requires_valid_token(client, monkeypatch):
    assert client.get("/api/ops/tc-desired").status_code == 401
    assert client.get("/api/ops/tc-desired",
                      headers={"Authorization": "Bearer thr_wrong"}).status_code == 401

    d = _mint(client)
    seen = {}

    def fake_export(pool, today):
        seen["uid"] = tenant.get_user()
        return {"version": 2, "plan_date": today, "tenant": seen["uid"], "groups": []}

    from tophat.services import tc_export
    monkeypatch.setattr(tc_export, "build_desired_state", fake_export)
    r = client.get("/api/ops/tc-desired",
                   headers={"Authorization": f"Bearer {d['token']}"})
    assert r.status_code == 200
    assert r.json()["tenant"] == 1 and seen["uid"] == 1   # token bound the tenant
    assert client.get("/api/boxes").json()["boxes"][0]["last_seen"] != ""


def test_tc_desired_surfaces_export_refusal_as_409(client, monkeypatch):
    d = _mint(client)
    from tophat.services import tc_export

    def refuse(pool, today):
        raise ValueError("today's copier plan has 2 unapplied line(s)")

    monkeypatch.setattr(tc_export, "build_desired_state", refuse)
    r = client.get("/api/ops/tc-desired",
                   headers={"Authorization": f"Bearer {d['token']}"})
    assert r.status_code == 409 and "unapplied" in r.json()["error"]


def test_tc_status_persists_and_notifies(client, monkeypatch):
    d = _mint(client)
    client.post("/api/settings",
                json={"discord_webhook_url": "https://discord.com/api/webhooks/1/x"})
    posts = []
    from tophat.services import notify
    monkeypatch.setattr(notify, "post_discord",
                        lambda url, title, **kw: posts.append((url, title)))

    status = {"plan_date": "2026-07-09", "result": "applied",
              "detail": "groups and feeds verified",
              "changes": {"groups_created": 1}, "verify": {"groups_ok": True}}
    r = client.post("/api/ops/tc-status", json=status,
                    headers={"Authorization": f"Bearer {d['token']}"})
    assert r.status_code == 200
    with tenant.as_user(1):
        stored = json.loads(tenant.resolve(TC_STATUS_FILE).read_text(encoding="utf-8"))
    assert stored["result"] == "applied"
    box = client.get("/api/boxes").json()
    assert box["boxes"][0]["last_result"] == "applied"
    assert box["boxes"][0]["last_plan_date"] == "2026-07-09"
    assert box["last_status"]["result"] == "applied"
    assert len(posts) == 1 and "applied" in posts[0][1]

    # noop stays silent on Discord but still persists
    r = client.post("/api/ops/tc-status",
                    json={"plan_date": "2026-07-09", "result": "noop", "detail": ""},
                    headers={"Authorization": f"Bearer {d['token']}"})
    assert r.status_code == 200 and len(posts) == 1


def test_revoke_kills_the_token(client):
    d = _mint(client)
    bid = client.get("/api/boxes").json()["boxes"][0]["box_id"]
    assert client.delete(f"/api/boxes/{bid}").json()["ok"] is True
    assert client.get("/api/boxes").json()["boxes"] == []
    r = client.get("/api/ops/tc-desired",
                   headers={"Authorization": f"Bearer {d['token']}"})
    assert r.status_code == 401


def test_copier_sync_toggle_makes_boxes_inert(client, monkeypatch):
    d = _mint(client)
    tok = {"Authorization": f"Bearer {d['token']}"}
    client.post("/api/settings", json={"copier_sync_enabled": False})
    from tophat.services import tc_export
    monkeypatch.setattr(tc_export, "build_desired_state",
                        lambda pool, today: (_ for _ in ()).throw(
                            AssertionError("exporter must not run while disabled")))
    r = client.get("/api/ops/tc-desired", headers=tok)
    assert r.status_code == 409 and "disabled in Settings" in r.json()["error"]
    client.post("/api/settings", json={"copier_sync_enabled": True})
    monkeypatch.setattr(tc_export, "build_desired_state",
                        lambda pool, today: {"version": 2, "plan_date": today,
                                             "tenant": 1, "groups": []})
    assert client.get("/api/ops/tc-desired", headers=tok).status_code == 200


def test_tc_poll_reports_apply_schedule(client):
    d = _mint(client)
    tok = {"Authorization": f"Bearer {d['token']}"}
    box = client.get("/api/boxes").json()["boxes"][0]
    assert box["apply_at"] == "" and box["seen_ago_s"] is None
    r = client.get("/api/ops/tc-poll?apply_at=22:00", headers=tok)
    assert r.status_code == 200
    box = client.get("/api/boxes").json()["boxes"][0]
    assert box["apply_at"] == "22:00"
    assert isinstance(box["seen_ago_s"], int) and box["seen_ago_s"] < 60
    # a poll without the param keeps the last known schedule
    client.get("/api/ops/tc-poll", headers=tok)
    assert client.get("/api/boxes").json()["boxes"][0]["apply_at"] == "22:00"


def test_sync_now_flag_set_seen_and_cleared_by_pull(client, monkeypatch):
    d = _mint(client)
    tok = {"Authorization": f"Bearer {d['token']}"}
    assert client.get("/api/ops/tc-poll", headers=tok).json() == {"sync_requested": False}
    bid = client.get("/api/boxes").json()["boxes"][0]["box_id"]
    assert client.post(f"/api/boxes/{bid}/sync").json()["ok"] is True
    assert client.get("/api/ops/tc-poll", headers=tok).json() == {"sync_requested": True}
    assert client.get("/api/boxes").json()["boxes"][0]["sync_requested"] is True
    from tophat.services import tc_export
    monkeypatch.setattr(tc_export, "build_desired_state",
                        lambda pool, today: {"version": 2, "plan_date": today,
                                             "tenant": 1, "groups": []})
    assert client.get("/api/ops/tc-desired", headers=tok).status_code == 200
    # the pull consumed the request
    assert client.get("/api/ops/tc-poll", headers=tok).json() == {"sync_requested": False}


def test_mark_applied_flags_every_box(client):
    _mint(client, "box one")
    _mint(client, "box two")
    r = client.post("/api/copier-plan/apply")
    assert r.status_code == 200 and r.json()["boxes_flagged"] == 2
    assert all(b["sync_requested"] for b in client.get("/api/boxes").json()["boxes"])


def test_tc_observed_roundtrip_books_and_stores(client):
    from datetime import datetime
    d = _mint(client)
    tok = {"Authorization": f"Bearer {d['token']}"}
    from tophat.store import mirrors as MS
    with tenant.as_user(1):
        m = MS.create_mirror("apex-50k", account_number="APEX-9")
        MS.patch_mirror(m.mirror_id, {"start_balance": 50_000.0, "days_traded": 1,
                                      "equity": 0.0}, today="2026-07-08")
    # nothing reported yet: the empty default still has the topology key
    assert client.get("/api/ops/tc-observed/last").json()["groups"] == []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    groups = [{"group": "TopHat PRAC-1", "status": "active", "leader": "PRAC-1",
               "followers": [{"account": "APEX-9", "scale": 1.0,
                              "replicate": True, "contract_type": "Standard"}]}]
    r = client.post("/api/ops/tc-observed", headers=tok, json={
        "version": 1, "generated_at": now,
        "accounts": [{"name": "APEX-9", "entity_id": "APEX-demo",
                      "entity_type": "demo", "entity_organization": "ApexTraderFunding",
                      "connected": True, "balance": 50_450.0, "realized_pnl": 0.0,
                      "week_realized_pnl": 0.0, "balance_sod": None,
                      "updated_at": now}],
        "groups": groups})
    assert r.status_code == 200
    out = r.json()
    assert out["summary"]["booked"] == 1
    with tenant.as_user(1):
        assert MS.load_mirrors()[m.mirror_id].equity == 450.0
    last = client.get("/api/ops/tc-observed/last").json()
    assert last["summary"]["booked"] == 1 and last["received_at"]
    assert last["accounts"][0]["name"] == "APEX-9"
    assert last["groups"] == groups   # topology stored verbatim for the UI


def test_tc_observed_one_click_import_filters_stale_accounts(client):
    from datetime import datetime
    from tophat.store import mirrors as MS

    d = _mint(client)
    tok = {"Authorization": f"Bearer {d['token']}"}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    r = client.post("/api/ops/tc-observed", headers=tok, json={
        "version": 1,
        "generated_at": now,
        "accounts": [
            {"name": "LFE-NEW", "entity_id": "lucid-live",
             "entity_type": "demo", "entity_organization": "LucidTrading",
             "connected": True, "balance": 50_125.0, "updated_at": now},
            {"name": "OLD-APEX", "entity_id": "APEX-old",
             "entity_type": "demo", "entity_organization": "ApexTraderFunding",
             "connected": False, "balance": 49_000.0,
             "updated_at": "2026-05-29 22:19:47"},
        ],
        "groups": [],
    })
    assert r.status_code == 200
    assert r.json()["summary"]["proposed"] == 1
    assert r.json()["summary"]["ignored"] == 1

    r = client.post("/api/ops/tc-observed/import",
                    json={"names": ["LFE-NEW", "OLD-APEX"]})
    assert r.status_code == 200
    out = r.json()
    assert [m["account_number"] for m in out["created"]] == ["LFE-NEW"]
    assert out["skipped"][0]["name"] == "OLD-APEX"
    assert out["summary"]["anchored"] == 1
    assert out["summary"]["ignored"] == 1
    with tenant.as_user(1):
        mirrors = list(MS.load_mirrors().values())
    assert len(mirrors) == 1
    assert mirrors[0].firm == "lucid-50k"
    assert mirrors[0].start_balance == 50_125.0

    # Repeating the click is safe and never creates a duplicate.
    again = client.post("/api/ops/tc-observed/import",
                        json={"names": ["LFE-NEW"]}).json()
    assert again["created"] == []
    assert again["skipped"] == [{"name": "LFE-NEW", "reason": "already imported"}]
