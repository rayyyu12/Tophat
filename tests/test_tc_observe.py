"""Reverse sync (§13): the box-side reader (rabbit/tc_apply.read_observed)
against the fixture DB, and the TopHat-side importer (services/tc_observe)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from rabbit import tc_apply
from tests.test_tc_apply import GOOSE, UID, make_db
from tophat.services.tc_observe import import_observed, import_proposed_accounts
from tophat.store import mirrors as MS

TODAY = "2026-07-09"
NOW = datetime(2026, 7, 9, 8, 0, 0)
FRESH = "2026-07-09 06:52:00.1234567-05:00"      # ~2h old vs NOW
STALE = "2026-05-29 22:19:47"                    # six weeks old


def obs(name, balance, *, updated_at=FRESH, entity_type="demo",
        org="ApexTraderFunding", entity_id="APEX-demo", **extra):
    return {"name": name, "entity_id": entity_id, "entity_type": entity_type,
            "entity_organization": org, "connected": True, "balance": balance,
            "realized_pnl": 0.0, "week_realized_pnl": 0.0, "balance_sod": None,
            "updated_at": updated_at, **extra}


def payload(*accounts):
    return {"version": 1, "generated_at": "2026-07-09T08:00:00-04:00",
            "guard": {"goose_version": GOOSE, "user_id": UID},
            "accounts": list(accounts)}


# ------------------------------------------------------------------ reader

def test_reader_skips_leaders_and_hidden_reports_stale(tmp_path):
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE accounts SET updated_at=? WHERE name='APEX-A'", (FRESH,))
    con.execute("UPDATE accounts SET updated_at=? WHERE name='APEX-B'", (STALE,))
    con.execute("UPDATE accounts SET is_hidden=1 WHERE name='APEX-C'")
    con.execute("INSERT INTO cash_balances (id, account_id, timestamp, amount,"
                " amount_sod) VALUES (1, 201, '2026-07-09 06:00', 50237.0, 50000.0)")
    con.execute("UPDATE accounts SET balance=50237.0 WHERE name='APEX-A'")
    con.commit(); con.close()

    out = tc_apply.read_observed(db, GOOSE, UID)
    names = {a["name"]: a for a in out["accounts"]}
    # leaders (projectx entity) and hidden accounts never travel
    assert set(names) == {"APEX-A", "APEX-B"}
    a = names["APEX-A"]
    assert a["balance"] == 50237.0 and a["balance_sod"] == 50000.0
    assert a["entity_organization"] == "ApexTraderFunding"
    assert a["updated_at"] == FRESH
    assert names["APEX-B"]["updated_at"] == STALE   # stale rows still reported
    assert out["guard"] == {"goose_version": GOOSE, "user_id": UID}


def test_reader_guard_mismatch_aborts(tmp_path):
    db = make_db(tmp_path)
    with pytest.raises(tc_apply.Abort, match="schema guard"):
        tc_apply.read_observed(db, 20990101000000, UID)
    with pytest.raises(tc_apply.Abort, match="user guard"):
        tc_apply.read_observed(db, GOOSE, "someone-else")


def test_reader_reports_copier_topology(tmp_path):
    """The observed payload carries the live groups verbatim (leaders included,
    even though leader ACCOUNTS are filtered from `accounts`) so the Operations
    page can render Tradecopia's actual arrangement."""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO groups (id, name, user_id, status) VALUES"
                " ('g1', 'TopHat PRAC-1', ?, 'active')", (UID,))
    con.execute("INSERT INTO group_leader_accounts (id, group_id, entity_id,"
                " account_name) VALUES (101, 'g1', 'topstepx-x', 'PRAC-1')")
    con.execute("INSERT INTO group_follower_accounts (id, group_id, entity_id,"
                " scale, contract_type, account_name, replicate)"
                " VALUES (202, 'g1', 'APEX-demo', 0.8, 'Standard', 'APEX-B', 0)")
    con.execute("INSERT INTO group_follower_accounts (id, group_id, entity_id,"
                " scale, contract_type, account_name, replicate)"
                " VALUES (201, 'g1', 'APEX-demo', 1.0, 'Standard', 'APEX-A', 1)")
    con.commit(); con.close()

    out = tc_apply.read_observed(db, GOOSE, UID)
    assert out["groups"] == [{
        "group": "TopHat PRAC-1", "status": "active", "leader": "PRAC-1",
        "followers": [   # sorted by account
            {"account": "APEX-A", "scale": 1.0, "replicate": True,
             "contract_type": "Standard"},
            {"account": "APEX-B", "scale": 0.8, "replicate": False,
             "contract_type": "Standard"},
        ]}]
    # leader accounts still absent from the balances list (§13.5)
    assert "PRAC-1" not in {a["name"] for a in out["accounts"]}


def test_reader_groups_empty_when_no_copier_setup(tmp_path):
    out = tc_apply.read_observed(make_db(tmp_path), GOOSE, UID)
    assert out["groups"] == []


# ------------------------------------------------------------------ importer

def test_books_fresh_balance_with_anchor():
    m = MS.create_mirror("apex-50k", account_number="APEX-A", leader_id=1)
    MS.patch_mirror(m.mirror_id, {"start_balance": 50_000.0, "days_traded": 3,
                                  "equity": 100.0}, today="2026-07-08")
    out = import_observed(payload(obs("APEX-A", 50_237.0)), today=TODAY, now=NOW)
    assert out["results"][0]["status"] == "booked"
    assert out["results"][0]["equity"] == 237.0
    m2 = MS.load_mirrors()[m.mirror_id]
    assert m2.equity == 237.0 and m2.last_verified == TODAY
    assert m2.peak >= 237.0


def test_auto_anchor_on_fresh_phase_only():
    m = MS.create_mirror("apex-50k", account_number="APEX-A")   # untraded
    out = import_observed(payload(obs("APEX-A", 50_356.4)), today=TODAY, now=NOW)
    assert out["results"][0]["status"] == "anchored"
    m2 = MS.load_mirrors()[m.mirror_id]
    assert m2.start_balance == 50_356.4 and m2.equity == 0.0
    assert m2.last_verified == TODAY


def test_mid_phase_without_anchor_refuses():
    m = MS.create_mirror("apex-50k", account_number="APEX-A")
    MS.patch_mirror(m.mirror_id, {"days_traded": 5, "equity": 800.0},
                    today="2026-07-08")
    out = import_observed(payload(obs("APEX-A", 50_900.0)), today=TODAY, now=NOW)
    assert out["results"][0]["status"] == "needs_anchor"
    m2 = MS.load_mirrors()[m.mirror_id]
    assert m2.equity == 800.0 and m2.last_verified == ""   # untouched


def test_stale_observation_displays_but_never_books():
    m = MS.create_mirror("apex-50k", account_number="APEX-A")
    MS.patch_mirror(m.mirror_id, {"start_balance": 50_000.0}, today="2026-07-08")
    out = import_observed(payload(obs("APEX-A", 49_000.0, updated_at=STALE)),
                          today=TODAY, now=NOW)
    assert out["results"][0]["status"] == "stale"
    assert MS.load_mirrors()[m.mirror_id].equity == 0.0    # untouched
    # unparseable timestamps are stale too - never trust what you can't date
    out = import_observed(payload(obs("APEX-A", 49_000.0, updated_at="???")),
                          today=TODAY, now=NOW)
    assert out["results"][0]["status"] == "stale"


def test_unknown_account_becomes_proposal_never_created():
    before = set(MS.load_mirrors())
    out = import_observed(payload(obs("PAAPEX363570000064", 50_356.4)),
                          today=TODAY, now=NOW)
    r = out["results"][0]
    assert r["status"] == "proposed" and r["firm"] == "apex-50k"
    assert r["phase_hint"] == "funded"                     # PA prefix
    assert r["importable"] is True and r["connected"] is True
    assert set(MS.load_mirrors()) == before                # nothing created


def test_unknown_stale_disconnected_or_unknown_firm_is_ignored():
    out = import_observed(payload(
        obs("OLD-APEX", 49_000.0, updated_at=STALE, connected=False),
        obs("MYSTERY-1", 50_000.0, org="Mystery Firm", entity_id="mystery"),
    ), today=TODAY, now=NOW)
    old, mystery = out["results"]
    assert old["status"] == "ignored" and old["importable"] is False
    assert "offline" in old["reason"] and "older than" in old["reason"]
    assert mystery["status"] == "ignored" and "could not be inferred" in mystery["reason"]
    assert out["summary"]["proposed"] == 0
    assert out["summary"]["ignored"] == 2


def test_matched_disconnected_account_never_books():
    m = MS.create_mirror("apex-50k", account_number="APEX-A")
    MS.patch_mirror(m.mirror_id, {"start_balance": 50_000.0, "equity": 125.0},
                    today="2026-07-08")
    out = import_observed(
        payload(obs("APEX-A", 49_000.0, connected=False)),
        today=TODAY, now=NOW)
    assert out["results"][0]["status"] == "disconnected"
    assert MS.load_mirrors()[m.mirror_id].equity == 125.0


def test_import_proposed_accounts_is_filtered_and_idempotent():
    p = payload(
        obs("LFE-NEW", 50_000.0, org="LucidTrading", entity_id="lucid-1"),
        obs("OLD-APEX", 49_000.0, updated_at=STALE, connected=False),
    )
    first = import_proposed_accounts(p, now=NOW)
    assert [m.account_number for m in first["created"]] == ["LFE-NEW"]
    assert first["created"][0].firm == "lucid-50k"
    assert first["created"][0].phase == "eval"
    assert first["skipped"] == [{
        "name": "OLD-APEX",
        "reason": "Tradecopia connection is offline; observation is older than 36 hours",
    }]

    second = import_proposed_accounts(p, now=NOW)
    assert second["created"] == []
    assert {row["name"] for row in second["skipped"]} == {"LFE-NEW", "OLD-APEX"}
    assert len(MS.load_mirrors()) == 1


def test_duplicate_account_numbers_error():
    MS.create_mirror("apex-50k", account_number="X-1")
    MS.create_mirror("lucid-50k", account_number="X-1")
    out = import_observed(payload(obs("X-1", 50_100.0)), today=TODAY, now=NOW)
    assert out["results"][0]["status"] == "error"
    assert "2 mirrors" in out["results"][0]["reason"]


def test_idempotent_reimport_and_summary():
    m = MS.create_mirror("apex-50k", account_number="APEX-A")
    MS.patch_mirror(m.mirror_id, {"start_balance": 50_000.0, "days_traded": 2,
                                  "equity": 50.0}, today="2026-07-08")
    mb = MS.create_mirror("apex-50k", account_number="APEX-B")
    MS.patch_mirror(mb.mirror_id, {"start_balance": 50_000.0}, today="2026-07-08")
    p = payload(obs("APEX-A", 50_237.0),
                obs("APEX-B", 50_000.0, updated_at=STALE),   # matched but stale
                obs("NEW-ACCT", 50_000.0),
                obs("PRAC-1", 141_437.93, entity_type="projectx"))
    out1 = import_observed(p, today=TODAY, now=NOW)
    out2 = import_observed(p, today=TODAY, now=NOW)
    assert out1["summary"] == out2["summary"] == {
        "booked": 1, "anchored": 0, "stale": 1, "disconnected": 0,
        "needs_anchor": 0, "proposed": 1, "ignored": 0,
        "skipped_leader": 1, "error": 0}
    assert MS.load_mirrors()[m.mirror_id].equity == 237.0


def test_freshness_window_boundary():
    m = MS.create_mirror("apex-50k", account_number="APEX-A")
    MS.patch_mirror(m.mirror_id, {"start_balance": 50_000.0}, today="2026-07-08")
    just_inside = (NOW - timedelta(hours=35)).strftime("%Y-%m-%d %H:%M:%S")
    just_outside = (NOW - timedelta(hours=37)).strftime("%Y-%m-%d %H:%M:%S")
    out = import_observed(payload(obs("APEX-A", 50_100.0, updated_at=just_inside)),
                          today=TODAY, now=NOW)
    assert out["results"][0]["status"] == "booked"
    out = import_observed(payload(obs("APEX-A", 50_200.0, updated_at=just_outside)),
                          today=TODAY, now=NOW)
    assert out["results"][0]["status"] == "stale"
