"""TopHat Rabbit writer engine (rabbit/tc_apply.py) against a fixture DB.

The DDL below is the live schema at goose 20260521000000 (rabbit/fixtures.md);
if the real schema guard ever trips, refresh both. No app, no Windows: the
AppController seam is faked, "boot" behavior is a hook on start().
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rabbit import tc_apply

UID = "9931744e-779b-4bdf-8782-1a0eebedc2ac"
GOOSE = 20260521000000

DDL = """
CREATE TABLE goose_db_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL, is_applied INTEGER NOT NULL,
  tstamp TIMESTAMP DEFAULT (datetime('now')));
CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL,
  name TEXT NOT NULL, balance REAL NOT NULL, realized_pn_l REAL, week_realized_pn_l REAL,
  created_at DATETIME, updated_at DATETIME, is_hidden BOOLEAN NOT NULL DEFAULT false);
CREATE TABLE entities (id TEXT, name TEXT, email TEXT, user_id TEXT NOT NULL,
  platform_user_id INTEGER, auth_token TEXT, auth_token_expiry DATETIME, status TEXT,
  created_at DATETIME, updated_at DATETIME, organization TEXT,
  type TEXT NOT NULL DEFAULT 'demo', platform_id TEXT NOT NULL DEFAULT '',
  is_connected NUMERIC DEFAULT 1, notifications_enabled NUMERIC DEFAULT 1,
  disconnected_at DATETIME, notification_preferences TEXT DEFAULT '{}',
  disconnect_reason TEXT DEFAULT 'automatic', first_name TEXT, last_name TEXT,
  street_address TEXT, block_crossed_trades NUMERIC DEFAULT 0,
  prevent_hedging NUMERIC DEFAULT 1, PRIMARY KEY (id));
CREATE TABLE groups (id TEXT, name TEXT NOT NULL, user_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT active, created_at DATETIME, updated_at DATETIME,
  disable_replication_on_reconcile NUMERIC DEFAULT 1,
  position_reconciler_enabled NUMERIC DEFAULT 1,
  auto_close_follower_positions NUMERIC DEFAULT 1,
  prevent_hedging NUMERIC DEFAULT 1, PRIMARY KEY (id));
CREATE TABLE group_leader_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT, created_at DATETIME, updated_at DATETIME, entity_id TEXT,
  account_name TEXT);
CREATE TABLE group_follower_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL, entity_id TEXT, scale REAL NOT NULL,
  contract_type TEXT NOT NULL, created_at DATETIME, updated_at DATETIME,
  account_name TEXT, replicate BOOLEAN DEFAULT TRUE,
  replication_disable_reason TEXT DEFAULT '');
CREATE TABLE feeds (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  account_id INTEGER NOT NULL, entity_id INTEGER NOT NULL, connection_type TEXT NOT NULL,
  connection_status TEXT NOT NULL, update_interval INTEGER NOT NULL,
  created_at DATETIME, updated_at DATETIME);
CREATE TABLE positions (id INTEGER, account_id INTEGER NOT NULL, net_pos INTEGER NOT NULL,
  net_price REAL NOT NULL, bought INTEGER NOT NULL, bought_value REAL NOT NULL,
  sold INTEGER NOT NULL, sold_value REAL NOT NULL, realized_pl REAL DEFAULT 0,
  unrealized_pl REAL DEFAULT 0, timestamp DATETIME NOT NULL, created_at DATETIME,
  updated_at DATETIME, exit NUMERIC DEFAULT false, group_tag TEXT, symbol TEXT,
  PRIMARY KEY (account_id, id));
CREATE TABLE cash_balances (id INTEGER, account_id INTEGER, timestamp DATETIME,
  amount REAL, realized_pn_l REAL, week_realized_pn_l REAL, archived NUMERIC,
  amount_sod REAL, created_at DATETIME, updated_at DATETIME,
  PRIMARY KEY (id, account_id));
"""

# fixture account fleet: two Topstep leaders + three Apex followers
ACCOUNTS = [
    (101, "topstepx-x", "PRAC-1"),
    (102, "topstepx-x", "PRAC-2"),
    (201, "APEX-demo", "APEX-A"),
    (202, "APEX-demo", "APEX-B"),
    (203, "APEX-demo", "APEX-C"),
]


def make_db(tmp_path: Path, accounts=None, extra_entity_user: str | None = None,
            goose: int = GOOSE) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "tc.db"
    con = sqlite3.connect(db)
    con.executescript(DDL)
    con.execute("INSERT INTO goose_db_version (version_id, is_applied) VALUES (?,1)",
                (goose,))
    con.execute("INSERT INTO entities (id, name, user_id, type, organization)"
                " VALUES ('topstepx-x','t',?,'projectx','topstepx')", (UID,))
    con.execute("INSERT INTO entities (id, name, user_id, type, organization)"
                " VALUES ('APEX-demo','a',?,'demo','ApexTraderFunding')",
                (extra_entity_user or UID,))
    for aid, eid, name in (accounts or ACCOUNTS):
        con.execute("INSERT INTO accounts (id, entity_id, name, balance) VALUES (?,?,?,50000)",
                    (aid, eid, name))
        con.execute("INSERT INTO feeds (name, account_id, entity_id, connection_type,"
                    " connection_status, update_interval) VALUES (?,?,?,?,?,2)",
                    (name, aid, eid, "balance_polling", "connected"))
    con.commit()
    con.close()
    return db


class FakeApp:
    """AppController stand-in. boot='rebuild' recreates feeds at start (what the
    real app does); boot='eat_group' deletes the newest group (a rejected write);
    boot='noop' touches nothing (an entity that never finishes reconnecting)."""

    def __init__(self, db: Path, boot: str = "rebuild", running: bool = True):
        self.db, self.boot, self.running = db, boot, running
        self.quits = self.starts = 0

    def is_running(self):
        return self.running

    def quit(self):
        self.quits += 1
        self.running = False

    def start(self):
        self.starts += 1
        self.running = True
        con = sqlite3.connect(self.db)
        if self.boot == "rebuild":
            have = {r[0] for r in con.execute("SELECT account_id FROM feeds")}
            for aid, eid, name in con.execute("SELECT id, entity_id, name FROM accounts"):
                if aid not in have:
                    con.execute(
                        "INSERT INTO feeds (name, account_id, entity_id, connection_type,"
                        " connection_status, update_interval) VALUES (?,?,?,?,?,2)",
                        (name, aid, eid, "balance_polling", "connected"))
        elif self.boot == "eat_group":
            gid = con.execute(
                "SELECT id FROM groups ORDER BY created_at DESC LIMIT 1").fetchone()
            if gid:
                con.execute("DELETE FROM group_follower_accounts WHERE group_id=?", gid)
                con.execute("DELETE FROM group_leader_accounts WHERE group_id=?", gid)
                con.execute("DELETE FROM groups WHERE id=?", gid)
        con.commit()
        con.close()


def cfg_for(db: Path, tmp_path: Path) -> dict:
    return {"db_path": str(db), "guard_goose": GOOSE, "guard_user": UID,
            "verify_settle_s": 0, "quit_timeout_s": 5,
            "backups_dir": str(tmp_path / "backups")}


def desired(groups, date="2026-07-09"):
    return {"version": 2, "plan_date": date, "groups": groups}


def grp(leader, *followers):
    return {"leader": leader,
            "followers": [{"account": a, "scale": s, "replicate": r}
                          for a, s, r in followers]}


def rows(db, sql):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def table_dump(db):
    out = {}
    for t in ("groups", "group_leader_accounts", "group_follower_accounts", "feeds"):
        out[t] = rows(db, f"SELECT * FROM {t} ORDER BY rowid")
    return out


# ------------------------------------------------------------------ happy path

def test_create_group_from_scratch(tmp_path):
    db = make_db(tmp_path)
    app = FakeApp(db)
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True),
                                   ("APEX-B", 0.8, False))]),
                      cfg_for(db, tmp_path), app, force_window=True)
    assert st["result"] == "applied", st["detail"]
    assert st["changes"] == {"groups_created": 1, "groups_dropped": 0,
                             "followers_upserted": 2, "followers_dropped": 0,
                             "feeds_cleared": 0}
    g = rows(db, "SELECT name, user_id, status, disable_replication_on_reconcile,"
                 " position_reconciler_enabled, auto_close_follower_positions,"
                 " prevent_hedging FROM groups")
    assert g == [("TopHat PRAC-1", UID, "active", 0, 1, 1, 1)]
    lead = rows(db, "SELECT id, entity_id, account_name FROM group_leader_accounts")
    assert lead == [(101, "topstepx-x", "PRAC-1")]
    fol = rows(db, "SELECT id, scale, replicate, replication_disable_reason,"
                   " account_name, contract_type FROM group_follower_accounts ORDER BY id")
    assert fol == [(201, 1.0, 1, "", "APEX-A", "Standard"),
                   (202, 0.8, 0, "user_manual", "APEX-B", "Standard")]
    # feeds are never touched (hard rule 3) - the seeded rows all survive
    assert sorted(r[0] for r in rows(db, "SELECT account_id FROM feeds")) \
        == [101, 102, 201, 202, 203]
    assert app.quits == 1 and app.starts == 1
    assert Path(st["backup_dir"]).exists()


def test_second_run_is_noop_and_uuid_stable(tmp_path):
    db = make_db(tmp_path)
    app = FakeApp(db)
    d = desired([grp("PRAC-1", ("APEX-A", 1.0, True))])
    st1 = tc_apply.run(d, cfg_for(db, tmp_path), app, force_window=True)
    gid1 = rows(db, "SELECT id FROM groups")[0][0]
    st2 = tc_apply.run(d, cfg_for(db, tmp_path), app, force_window=True)
    assert (st1["result"], st2["result"]) == ("applied", "noop")
    assert rows(db, "SELECT id FROM groups")[0][0] == gid1
    assert app.quits == 1                       # noop never touches the app


def test_move_scale_and_replicate_reuse_group(tmp_path):
    db = make_db(tmp_path)
    app = FakeApp(db)
    cfg = cfg_for(db, tmp_path)
    tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True), ("APEX-B", 1.0, True))]),
                 cfg, app, force_window=True)
    gid = rows(db, "SELECT id FROM groups")[0][0]
    prac1_feed = rows(db, "SELECT id FROM feeds WHERE account_id=101")[0][0]
    # move APEX-B under a NEW leader PRAC-2, flip APEX-A off, change its scale
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 0.5, False)),
                               grp("PRAC-2", ("APEX-B", 1.0, True))]),
                      cfg, app, force_window=True)
    assert st["result"] == "applied", st["detail"]
    assert st["changes"]["groups_created"] == 1 and st["changes"]["groups_dropped"] == 0
    gids = dict(rows(db, "SELECT account_name, group_id FROM group_leader_accounts"))
    assert gids["PRAC-1"] == gid                 # reused, not recreated
    fol = {r[0]: r for r in rows(
        db, "SELECT account_name, group_id, scale, replicate,"
            " replication_disable_reason FROM group_follower_accounts")}
    assert fol["APEX-A"][1] == gid and fol["APEX-A"][2:] == (0.5, 0, "user_manual")
    assert fol["APEX-B"][1] == gids["PRAC-2"] and fol["APEX-B"][3] == 1
    # PRAC-1 kept its group -> its feed row was never cleared (same rowid)
    assert rows(db, "SELECT id FROM feeds WHERE account_id=101")[0][0] == prac1_feed


def test_absence_unmaps_and_drops_group(tmp_path):
    db = make_db(tmp_path)
    app = FakeApp(db)
    cfg = cfg_for(db, tmp_path)
    tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True)),
                          grp("PRAC-2", ("APEX-B", 1.0, True))]),
                 cfg, app, force_window=True)
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg, app, force_window=True)
    assert st["result"] == "applied"
    assert st["changes"]["groups_dropped"] == 1
    assert st["changes"]["followers_dropped"] == 1
    assert rows(db, "SELECT account_name FROM group_leader_accounts") == [("PRAC-1",)]
    assert rows(db, "SELECT account_name FROM group_follower_accounts") == [("APEX-A",)]
    # empty desired state = clean slate
    st = tc_apply.run(desired([]), cfg, app, force_window=True)
    assert st["result"] == "applied"
    assert rows(db, "SELECT COUNT(*) FROM groups")[0][0] == 0
    assert rows(db, "SELECT COUNT(*) FROM group_follower_accounts")[0][0] == 0


# ------------------------------------------------------------------ guards

@pytest.mark.parametrize("mutate,needle", [
    (dict(goose=20990101000000), "schema guard"),
    (dict(extra_entity_user="someone-else"), "user guard"),
])
def test_guards_abort_before_touching_the_app(tmp_path, mutate, needle):
    db = make_db(tmp_path, **mutate)
    app = FakeApp(db)
    before = table_dump(db)
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg_for(db, tmp_path), app, force_window=True)
    assert st["result"] == "aborted" and needle in st["detail"]
    assert app.quits == 0 and table_dump(db) == before


def test_unknown_and_duplicate_names_abort(tmp_path):
    db = make_db(tmp_path)
    app = FakeApp(db)
    st = tc_apply.run(desired([grp("PRAC-1", ("NOT-CONNECTED", 1.0, True))]),
                      cfg_for(db, tmp_path), app, force_window=True)
    assert st["result"] == "aborted" and "not connected" in st["detail"]
    db2 = make_db(tmp_path / "d2", accounts=ACCOUNTS + [(299, "APEX-demo", "APEX-A")])
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg_for(db2, tmp_path / "d2"), FakeApp(db2), force_window=True)
    assert st["result"] == "aborted" and "ambiguous" in st["detail"]


def test_open_position_aborts(tmp_path):
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO positions (id, account_id, net_pos, net_price, bought,"
                " bought_value, sold, sold_value, timestamp)"
                " VALUES (1, 201, 2, 20000, 2, 40000, 0, 0, '2026-07-08')")
    con.commit(); con.close()
    app = FakeApp(db)
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg_for(db, tmp_path), app, force_window=True)
    assert st["result"] == "aborted" and "open position" in st["detail"]
    assert app.quits == 0


def test_second_writer_lock_aborts(tmp_path):
    db = make_db(tmp_path)
    lock = Path(str(db) + ".rabbit-lock")
    lock.write_text("123")
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg_for(db, tmp_path), FakeApp(db), force_window=True)
    assert st["result"] == "aborted" and "another writer" in st["detail"]
    lock.unlink()


def test_blackout_guard(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    cfg = {"blackout_start": "09:30", "blackout_end": "16:10"}
    et = ZoneInfo("America/New_York")
    assert tc_apply.in_blackout(cfg, datetime(2026, 7, 9, 12, 0, tzinfo=et))
    assert not tc_apply.in_blackout(cfg, datetime(2026, 7, 9, 22, 0, tzinfo=et))
    assert not tc_apply.in_blackout(cfg, datetime(2026, 7, 9, 7, 0, tzinfo=et))


def test_parse_desired_rejects_bad_shapes():
    with pytest.raises(tc_apply.Abort, match="version"):
        tc_apply.parse_desired({"version": 9, "plan_date": "x", "groups": []})
    with pytest.raises(tc_apply.Abort, match="twice"):
        tc_apply.parse_desired(desired([grp("L1", ("A", 1.0, True)),
                                        grp("L2", ("A", 1.0, True))]))
    with pytest.raises(tc_apply.Abort, match="both leader and follower"):
        tc_apply.parse_desired(desired([grp("L1", ("L1", 1.0, True))]))
    with pytest.raises(tc_apply.Abort, match="no followers"):
        tc_apply.parse_desired(desired([{"leader": "L1", "followers": []}]))


def test_fleet_scale_apply_and_reassign(tmp_path):
    """Steady-state fleet shape: 12 leader groups, 30 followers, one txn. Then a
    bulk reassignment (every follower moves to a different leader) applies as a
    minimal diff with no leftover rows - the 'reassign a ton of evals at once'
    case."""
    accounts = ([(100 + i, "topstepx-x", f"TS-{i:02d}") for i in range(1, 13)]
                + [(200 + i, "APEX-demo", f"FOL-{i:02d}") for i in range(1, 31)])
    db = make_db(tmp_path, accounts=accounts)
    app = FakeApp(db)
    fol = iter(range(1, 31))
    d1 = desired([grp(f"TS-{gi:02d}",
                      *[(f"FOL-{next(fol):02d}", 1.0, True)
                        for _ in range(3 if gi <= 6 else 2)])
                  for gi in range(1, 13)])       # 6x3 + 6x2 = 30 followers
    st = tc_apply.run(d1, cfg_for(db, tmp_path), app, force_window=True)
    assert st["result"] == "applied", st["detail"]
    assert st["changes"] == {"groups_created": 12, "groups_dropped": 0,
                             "followers_upserted": 30, "followers_dropped": 0,
                             "feeds_cleared": 0}
    assert len(rows(db, "SELECT id FROM groups")) == 12
    assert len(rows(db, "SELECT id FROM group_follower_accounts")) == 30
    # re-run -> noop (group identity stable at scale)
    assert tc_apply.run(d1, cfg_for(db, tmp_path), app,
                        force_window=True)["result"] == "noop"
    # rotate every follower onto the NEXT leader: 30 moves, groups reused
    fol = iter(range(1, 31))
    d2 = desired([grp(f"TS-{(gi % 12) + 1:02d}",
                      *[(f"FOL-{next(fol):02d}", 1.0, True)
                        for _ in range(3 if gi <= 6 else 2)])
                  for gi in range(1, 13)])
    st2 = tc_apply.run(d2, cfg_for(db, tmp_path), app, force_window=True)
    assert st2["result"] == "applied", st2["detail"]
    assert st2["changes"]["groups_created"] == 0
    assert st2["changes"]["followers_upserted"] == 30
    assert len(rows(db, "SELECT id FROM group_follower_accounts")) == 30
    assert len(rows(db, "SELECT DISTINCT account_id FROM feeds")) == 42


# ------------------------------------------------------------------ failure paths

def test_sql_error_mid_txn_restores_backup(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    app = FakeApp(db)
    before = table_dump(db)

    def sabotage(con, ops, guard_user):
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM feeds")         # partial damage, then blow up
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    monkeypatch.setattr(tc_apply, "apply_ops", sabotage)
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg_for(db, tmp_path), app, force_window=True)
    assert st["result"] == "aborted" and "backup restored" in st["detail"]
    assert table_dump(db) == before
    assert app.starts == 1                        # app brought back up


def test_verify_failure_rolls_back(tmp_path):
    db = make_db(tmp_path)
    app = FakeApp(db, boot="eat_group")           # app "rejects" the write at boot
    before = table_dump(db)
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg_for(db, tmp_path), app, force_window=True)
    assert st["result"] == "rolled_back"
    assert "missing after boot" in st["detail"]
    assert table_dump(db) == before               # backup restored
    assert app.starts == 2                        # boot for verify + boot after restore


def test_apply_verifies_and_never_touches_feeds(tmp_path):
    """The updated Tradecopia (goose 20260628000001) keeps an EMPTY feeds table
    even in steady state and never rebuilds deleted rows, so (a) verify must
    pass on intact copier rows alone (requiring feeds rolled back two correct
    applies on 2026-07-13) and (b) the writer must not delete feeds (deleting
    them killed replication: copied orders sat PendingNew, 2026-07-14)."""
    db = make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM feeds")               # the live box's actual state
    con.commit(); con.close()
    app = FakeApp(db, boot="noop")                 # boot never recreates feeds
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg_for(db, tmp_path), app, force_window=True)
    assert st["result"] == "applied", st["detail"]
    assert st["changes"]["feeds_cleared"] == 0
    assert rows(db, "SELECT id FROM feeds") == []  # untouched either way


def test_settle_window_catches_late_group_deletion(tmp_path, monkeypatch):
    """Rows are committed before the relaunch, so the risk verify guards is the
    app DELETING them during boot reconciliation - which can land seconds in.
    The settle window must keep watching and roll back on late damage."""
    db = make_db(tmp_path)
    app = FakeApp(db, boot="noop")
    before = table_dump(db)

    def reconciliation_rejects(_seconds):          # fires between two polls
        FakeApp(db, boot="eat_group").start()

    monkeypatch.setattr(tc_apply.time, "sleep", reconciliation_rejects)
    cfg = dict(cfg_for(db, tmp_path), verify_settle_s=60)
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg, app, force_window=True)
    assert st["result"] == "rolled_back"
    assert "missing after boot" in st["detail"]
    assert table_dump(db) == before                # backup restored


def test_dry_mode_plans_without_touching(tmp_path):
    db = make_db(tmp_path)
    app = FakeApp(db)
    before = table_dump(db)
    st = tc_apply.run(desired([grp("PRAC-1", ("APEX-A", 1.0, True))]),
                      cfg_for(db, tmp_path), app, force_window=True, dry=True)
    assert st["result"] == "planned" and "CREATE group" in st["detail"]
    assert table_dump(db) == before and app.quits == 0
