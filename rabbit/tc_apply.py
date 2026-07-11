"""tc-apply — the Tradecopia DB writer engine (TopHat Rabbit's hands).

Implements docs/TRADECOPIA_AUTOMATION_PLAN.md §7 against the facts recorded in
rabbit/fixtures.md. Standard library only. The hard rules (§10), enforced here:

  1. never write while the Tradecopia process exists (exclusive-lock check)
  2. never touch accounts / entities / tokens
  3. never insert feeds — delete and let boot rebuild
  4. never force-kill the app
  5. never write without a same-run backup of db+wal+shm
  6. guard mismatch (schema version, user id, names, open position) = abort
  7. all writes in ONE transaction
  8. desired state is declarative and complete; this module computes the diff

`run()` is the only entry point that writes anything. It is deliberately
synchronous and boring: preflight -> quit app -> backup -> transaction ->
relaunch -> verify -> (rollback on failure) -> status dict (§5.2 shape).
The app-control seam (`AppController`) is injectable so the whole engine is
testable against a fixture DB with no app and no Windows.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# Fixture-locked conventions (rabbit/fixtures.md; re-verify if the schema guard trips)
GROUP_FLAGS = {"disable_replication_on_reconcile": 0, "position_reconciler_enabled": 1,
               "auto_close_follower_positions": 1, "prevent_hedging": 1}
REPL_OFF_REASON = "user_manual"
DB_SIBLINGS = ("", "-wal", "-shm")


class Abort(Exception):
    """Preflight/guard failure: nothing was written, app untouched (unless noted)."""


def now_stamp() -> str:
    """Go-shaped local timestamp: '2026-07-08 15:42:01.1234560-05:00' —
    7-digit fraction + utc offset, matching every app-written row."""
    now = datetime.now().astimezone()
    off = now.strftime("%z")
    return (f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond * 10:07d}"
            f"{off[:3]}:{off[3:]}")


# ------------------------------------------------------------- desired state

@dataclass(frozen=True)
class FollowerSpec:
    account: str
    scale: float
    replicate: bool
    contract_type: str


@dataclass(frozen=True)
class GroupSpec:
    leader: str
    followers: tuple[FollowerSpec, ...]


def parse_desired(d: dict) -> tuple[str, list[GroupSpec]]:
    """Validate the §5.1 payload shape. Raises Abort on anything off."""
    if not isinstance(d, dict) or int(d.get("version") or 0) not in (1, 2):
        raise Abort(f"unsupported desired-state version {d.get('version')!r}")
    plan_date = str(d.get("plan_date") or "")
    if not plan_date:
        raise Abort("desired state has no plan_date")
    groups: list[GroupSpec] = []
    seen_leaders: set[str] = set()
    seen_followers: set[str] = set()
    for g in d.get("groups") or []:
        leader = str(g.get("leader") or "").strip()
        if not leader:
            raise Abort("group with empty leader name")
        if leader in seen_leaders:
            raise Abort(f"leader {leader!r} appears in two groups")
        seen_leaders.add(leader)
        fs = []
        for f in g.get("followers") or []:
            acct = str(f.get("account") or "").strip()
            if not acct:
                raise Abort(f"group {leader!r}: follower with empty account")
            if acct in seen_followers:
                raise Abort(f"follower {acct!r} appears twice in the desired state")
            seen_followers.add(acct)
            fs.append(FollowerSpec(
                account=acct, scale=float(f.get("scale", 1.0)),
                replicate=bool(f.get("replicate", True)),
                contract_type=str(f.get("contract_type") or "Standard")))
        if not fs:
            raise Abort(f"group {leader!r} has no followers - omit it instead")
        groups.append(GroupSpec(leader=leader, followers=tuple(fs)))
    overlap = seen_leaders & seen_followers
    if overlap:
        raise Abort(f"account(s) both leader and follower: {sorted(overlap)}")
    return plan_date, groups


# ------------------------------------------------------------- db snapshot

@dataclass
class DbState:
    goose: int
    user_ids: set[str]
    accounts_by_name: dict[str, dict]          # name -> {id, entity_id, name}
    dup_names: set[str]
    groups: dict[str, dict]                    # group_id -> row dict
    leaders: dict[str, dict]                   # group_id -> leader row (id = accounts.id)
    followers: dict[int, dict]                 # follower accounts.id -> row
    feed_account_ids: set[int]
    open_positions: dict[int, int]             # account_id -> net_pos (non-zero only)


def open_ro(db_path: str | Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def read_state(con: sqlite3.Connection) -> DbState:
    cur = con.cursor()
    goose = int(cur.execute("SELECT MAX(version_id) FROM goose_db_version").fetchone()[0] or 0)
    user_ids = {r[0] for r in cur.execute(
        "SELECT user_id FROM entities UNION SELECT user_id FROM groups")}
    accounts: dict[str, dict] = {}
    dup: set[str] = set()
    for aid, eid, name in cur.execute("SELECT id, entity_id, name FROM accounts"):
        if name in accounts:
            dup.add(name)
        accounts[name] = {"id": int(aid), "entity_id": eid, "name": name}
    groups = {r[0]: {"id": r[0], "name": r[1], "user_id": r[2], "status": r[3]}
              for r in cur.execute("SELECT id, name, user_id, status FROM groups")}
    leaders = {r[1]: {"id": int(r[0]), "group_id": r[1], "entity_id": r[2],
                      "account_name": r[3]}
               for r in cur.execute(
                   "SELECT id, group_id, entity_id, account_name FROM group_leader_accounts")}
    followers = {int(r[0]): {"id": int(r[0]), "group_id": r[1], "entity_id": r[2],
                             "scale": float(r[3]), "contract_type": r[4],
                             "account_name": r[5], "replicate": int(r[6] or 0)}
                 for r in cur.execute(
                     "SELECT id, group_id, entity_id, scale, contract_type,"
                     " account_name, replicate FROM group_follower_accounts")}
    feeds = {int(r[0]) for r in cur.execute("SELECT account_id FROM feeds")}
    positions = {int(r[0]): int(r[1]) for r in cur.execute(
        "SELECT account_id, net_pos FROM positions WHERE net_pos != 0")}
    return DbState(goose=goose, user_ids=user_ids, accounts_by_name=accounts,
                   dup_names=dup, groups=groups, leaders=leaders,
                   followers=followers, feed_account_ids=feeds,
                   open_positions=positions)


# ------------------------------------------------------------- diff

@dataclass
class Ops:
    """The exact row operations one run will perform (empty = noop)."""
    groups_create: list[dict] = field(default_factory=list)   # {group_id, name, leader(acct-row)}
    groups_drop: list[str] = field(default_factory=list)      # group_ids
    follower_upserts: list[dict] = field(default_factory=list)  # target row values
    follower_deletes: list[int] = field(default_factory=list)   # follower accounts.id
    feeds_clear: set[int] = field(default_factory=set)           # accounts.id
    group_of_leader: dict[str, str] = field(default_factory=dict)  # leader name -> group_id
    managed_accounts: set[int] = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return not (self.groups_create or self.groups_drop
                    or self.follower_upserts or self.follower_deletes)

    def summary(self) -> dict:
        return {"groups_created": len(self.groups_create),
                "groups_dropped": len(self.groups_drop),
                "followers_upserted": len(self.follower_upserts),
                "followers_dropped": len(self.follower_deletes),
                "feeds_cleared": len(self.feeds_clear)}


def resolve(desired: list[GroupSpec], st: DbState, guard_goose: int,
            guard_user: str) -> Ops:
    """Guards + name resolution + minimal diff. Raises Abort; never writes."""
    if st.goose != int(guard_goose):
        raise Abort(f"schema guard: goose_db_version {st.goose} != expected "
                    f"{guard_goose} - Tradecopia updated, re-run Stage 0 recon")
    if st.user_ids != {guard_user}:
        raise Abort(f"user guard: DB user ids {sorted(st.user_ids)} != "
                    f"[{guard_user!r}]")

    def acct(name: str) -> dict:
        if name in st.dup_names:
            raise Abort(f"account name {name!r} is ambiguous in Tradecopia")
        row = st.accounts_by_name.get(name)
        if row is None:
            raise Abort(f"account {name!r} not connected in Tradecopia "
                        "(new accounts are onboarded by hand, never by the writer)")
        return row

    ops = Ops()
    desired_follower_ids: set[int] = set()
    desired_leader_ids: set[int] = set()

    for g in desired:
        leader = acct(g.leader)
        desired_leader_ids.add(leader["id"])
        # reuse the leader's existing group (stable identity, minimal feed churn)
        existing_gid = next((gid for gid, lrow in st.leaders.items()
                             if lrow["id"] == leader["id"]), None)
        if existing_gid is None:
            gid = str(uuid.uuid4())
            ops.groups_create.append({"group_id": gid,
                                      "name": f"TopHat {leader['name']}",
                                      "leader": leader})
            ops.feeds_clear.add(leader["id"])
        else:
            gid = existing_gid
        ops.group_of_leader[g.leader] = gid
        ops.managed_accounts.add(leader["id"])

        for f in g.followers:
            frow = acct(f.account)
            if frow["id"] in desired_follower_ids:
                raise Abort(f"follower {f.account!r} resolved twice")
            desired_follower_ids.add(frow["id"])
            ops.managed_accounts.add(frow["id"])
            target = {"id": frow["id"], "group_id": gid,
                      "entity_id": frow["entity_id"], "scale": float(f.scale),
                      "contract_type": f.contract_type,
                      "account_name": frow["name"],
                      "replicate": 1 if f.replicate else 0,
                      "replication_disable_reason": "" if f.replicate else REPL_OFF_REASON}
            cur = st.followers.get(frow["id"])
            changed = (cur is None
                       or cur["group_id"] != target["group_id"]
                       or abs(cur["scale"] - target["scale"]) > 1e-9
                       or cur["contract_type"] != target["contract_type"]
                       or cur["replicate"] != target["replicate"])
            if changed:
                ops.follower_upserts.append(target)
                ops.feeds_clear.add(frow["id"])

    # drops: current followers not desired anywhere
    for fid, row in st.followers.items():
        if fid not in desired_follower_ids:
            ops.follower_deletes.append(fid)
            ops.feeds_clear.add(fid)
    # drops: groups whose leader is not a desired leader
    for gid, lrow in st.leaders.items():
        if lrow["id"] not in desired_leader_ids:
            ops.groups_drop.append(gid)
            ops.feeds_clear.add(lrow["id"])
    # a group row without any leader row is an orphan the app would auto-delete;
    # sweep it too so we never leave one behind
    for gid in st.groups:
        if gid not in st.leaders and gid not in ops.groups_drop:
            ops.groups_drop.append(gid)

    # flat guard (fixtures addition): refuse if the DB shows an open position
    # on any account this run touches
    open_touched = {aid: pos for aid, pos in st.open_positions.items()
                    if aid in ops.managed_accounts
                    or aid in {f for f in ops.follower_deletes}}
    if open_touched and not ops.empty:
        raise Abort(f"open position(s) in DB for touched account(s) {open_touched} "
                    "- refusing to rearrange")
    ops.follower_deletes.sort()
    ops.groups_drop.sort()
    return ops


def describe(ops: Ops) -> list[str]:
    """Human-readable diff lines for `plan` mode / logs."""
    out = []
    for g in ops.groups_create:
        out.append(f"CREATE group {g['name']!r} led by {g['leader']['name']}")
    for gid in ops.groups_drop:
        out.append(f"DROP group {gid} (+ its leader/follower rows)")
    for f in ops.follower_upserts:
        out.append(f"UPSERT follower {f['account_name']} -> group {f['group_id'][:8]}… "
                   f"scale={f['scale']} replicate={f['replicate']}")
    for fid in ops.follower_deletes:
        out.append(f"DELETE follower row {fid}")
    if ops.feeds_clear:
        out.append(f"CLEAR feeds for accounts {sorted(ops.feeds_clear)}")
    return out or ["no changes - desired state already in place"]


# ------------------------------------------------------------- write

def apply_ops(con: sqlite3.Connection, ops: Ops, guard_user: str) -> None:
    """One IMMEDIATE transaction; caller owns the connection and rollback."""
    ts = now_stamp()
    cur = con.cursor()
    cur.execute("BEGIN IMMEDIATE")
    for g in ops.groups_create:
        cur.execute(
            "INSERT INTO groups (id, name, user_id, status, created_at, updated_at,"
            " disable_replication_on_reconcile, position_reconciler_enabled,"
            " auto_close_follower_positions, prevent_hedging)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (g["group_id"], g["name"], guard_user, "active", ts, ts,
             GROUP_FLAGS["disable_replication_on_reconcile"],
             GROUP_FLAGS["position_reconciler_enabled"],
             GROUP_FLAGS["auto_close_follower_positions"],
             GROUP_FLAGS["prevent_hedging"]))
        lead = g["leader"]
        cur.execute(
            "INSERT INTO group_leader_accounts (id, group_id, created_at,"
            " updated_at, entity_id, account_name) VALUES (?,?,?,?,?,?)",
            (lead["id"], g["group_id"], ts, ts, lead["entity_id"], lead["name"]))
    for f in ops.follower_upserts:
        cur.execute("DELETE FROM group_follower_accounts WHERE id = ?", (f["id"],))
        cur.execute(
            "INSERT INTO group_follower_accounts (id, group_id, entity_id, scale,"
            " contract_type, created_at, updated_at, account_name, replicate,"
            " replication_disable_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f["id"], f["group_id"], f["entity_id"], f["scale"],
             f["contract_type"], ts, ts, f["account_name"], f["replicate"],
             f["replication_disable_reason"]))
    for fid in ops.follower_deletes:
        cur.execute("DELETE FROM group_follower_accounts WHERE id = ?", (fid,))
    for gid in ops.groups_drop:
        # children first: never leave a group without its leader row mid-flight
        cur.execute("DELETE FROM group_follower_accounts WHERE group_id = ?", (gid,))
        cur.execute("DELETE FROM group_leader_accounts WHERE group_id = ?", (gid,))
        cur.execute("DELETE FROM groups WHERE id = ?", (gid,))
    for aid in sorted(ops.feeds_clear):
        cur.execute("DELETE FROM feeds WHERE account_id = ?", (aid,))
    con.commit()


# ------------------------------------------------------------- verify

def verify_state(db_path: str | Path, desired: list[GroupSpec],
                 ops: Ops) -> tuple[bool, str]:
    """Post-boot acceptance: our rows survived reconciliation and feeds exist."""
    con = open_ro(db_path)
    try:
        st = read_state(con)
    finally:
        con.close()
    for g in desired:
        gid = ops.group_of_leader.get(g.leader)
        if gid is None or gid not in st.groups:
            return False, f"group for leader {g.leader!r} missing after boot"
        lrow = st.leaders.get(gid)
        if lrow is None or lrow["account_name"] != g.leader:
            return False, f"leader row for {g.leader!r} missing after boot"
        for f in g.followers:
            arow = st.accounts_by_name.get(f.account)
            frow = st.followers.get(arow["id"]) if arow else None
            if (frow is None or frow["group_id"] != gid
                    or abs(frow["scale"] - f.scale) > 1e-9
                    or frow["replicate"] != (1 if f.replicate else 0)):
                return False, f"follower row for {f.account!r} wrong/missing after boot"
    missing_feeds = {aid for aid in ops.managed_accounts
                     if aid not in st.feed_account_ids}
    if missing_feeds:
        return False, f"feeds not rebuilt for accounts {sorted(missing_feeds)}"
    return True, "groups and feeds verified"


# ------------------------------------------------------------- backup

def make_backup(db_path: Path, backups_dir: Path, plan_date: str) -> Path:
    dest = backups_dir / f"{plan_date}_{datetime.now():%H%M%S}"
    n = 1
    while dest.exists():   # second run within the same second (retries, tests)
        n += 1
        dest = dest.with_name(f"{dest.name.rsplit('-', 1)[0]}-{n}"
                              if n > 2 else f"{dest.name}-{n}")
    dest.mkdir(parents=True, exist_ok=False)
    for suffix in DB_SIBLINGS:
        src = Path(str(db_path) + suffix)
        if src.exists():
            shutil.copy2(src, dest / src.name)
    return dest


def restore_backup(db_path: Path, backup_dir: Path) -> None:
    """Put back exactly the backed-up file set (extra -wal/-shm are removed)."""
    for suffix in DB_SIBLINGS:
        live = Path(str(db_path) + suffix)
        saved = backup_dir / live.name
        if saved.exists():
            shutil.copy2(saved, live)
        elif live.exists():
            live.unlink()


def prune_backups(backups_dir: Path, keep_days: int = 30) -> None:
    cutoff = datetime.now() - timedelta(days=keep_days)
    if not backups_dir.exists():
        return
    for d in backups_dir.iterdir():
        try:
            if d.is_dir() and datetime.fromtimestamp(d.stat().st_mtime) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ------------------------------------------------------------- app control

class AppController:
    """Windows Tradecopia process control. Injectable seam for tests."""

    def __init__(self, exe: str, process_name: str = "Tradecopia.exe"):
        self.exe = exe
        self.process_name = process_name

    def is_running(self) -> bool:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {self.process_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15).stdout
        return self.process_name in out

    def quit(self) -> None:
        # graceful (WM_CLOSE); NEVER /F in v1 (§10.4)
        subprocess.run(["taskkill", "/IM", self.process_name],
                       capture_output=True, text=True, timeout=15)

    def start(self) -> None:
        subprocess.Popen([self.exe], cwd=str(Path(self.exe).parent),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def wait_exclusive(db_path: Path, app: AppController, timeout_s: float) -> bool:
    """True once the process tree is gone AND the DB accepts BEGIN IMMEDIATE."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not app.is_running():
            try:
                con = sqlite3.connect(db_path, timeout=1)
                try:
                    con.execute("BEGIN IMMEDIATE")
                    con.rollback()
                    return True
                finally:
                    con.close()
            except sqlite3.OperationalError:
                pass
        time.sleep(1.0)
    return False


# ------------------------------------------------------------- reverse sync (§13)

def read_observed(db_path: str | Path, guard_goose: int, guard_user: str, *,
                  include_leader_entities: bool = False) -> dict:
    """SELECT-only pull of account facts for TopHat (plan doc §13.3). Safe to
    run while the app is up (read-only open, no locks taken). Raises Abort on
    a guard mismatch — the caller sends an error status and NO accounts.

    Values are copied verbatim; TopHat owns all interpretation (freshness,
    anchors, onboarding). `entity_organization` is included beyond the §13.3
    sketch so the server can infer the firm for onboarding proposals, and
    `groups` carries the live copier topology (leader -> followers, scales)
    so the Operations page can show Tradecopia's actual arrangement."""
    con = open_ro(db_path)
    try:
        cur = con.cursor()
        goose = int(cur.execute(
            "SELECT MAX(version_id) FROM goose_db_version").fetchone()[0] or 0)
        if goose != int(guard_goose):
            raise Abort(f"schema guard: goose {goose} != expected {guard_goose}")
        user_ids = {r[0] for r in cur.execute(
            "SELECT user_id FROM entities UNION SELECT user_id FROM groups")}
        if user_ids and user_ids != {guard_user}:
            raise Abort(f"user guard: DB user ids {sorted(user_ids)}")
        entities = {r[0]: {"type": r[1], "organization": r[2] or "",
                           "connected": bool(r[3])}
                    for r in cur.execute(
                        "SELECT id, type, organization, is_connected FROM entities")}
        # latest start-of-day balance per account (informational)
        sod = {int(r[0]): r[1] for r in cur.execute(
            "SELECT account_id, amount_sod FROM cash_balances"
            " GROUP BY account_id HAVING MAX(timestamp)")}
        accounts = []
        for aid, eid, name, bal, day, week, upd, hidden in cur.execute(
                "SELECT id, entity_id, name, balance, realized_pn_l,"
                " week_realized_pn_l, updated_at, is_hidden FROM accounts"):
            ent = entities.get(eid, {"type": "?", "organization": "", "connected": False})
            if hidden:
                continue
            if ent["type"] == "projectx" and not include_leader_entities:
                continue   # leaders are API-authoritative in TopHat (§13.5)
            accounts.append({
                "name": name,
                "entity_id": eid,
                "entity_type": ent["type"],
                "entity_organization": ent["organization"],
                "connected": ent["connected"],
                "balance": float(bal),
                "realized_pnl": float(day or 0.0),
                "week_realized_pnl": float(week or 0.0),
                "balance_sod": (float(sod[aid]) if aid in sod else None),
                "updated_at": str(upd or ""),
            })
        # live copier topology - group tables carry account names directly, so
        # no join through the accounts filter above (leaders included by design)
        followers_by_group: dict[str, list[dict]] = {}
        for gid, fname, scale, repl, ctype in cur.execute(
                "SELECT group_id, account_name, scale, replicate, contract_type"
                " FROM group_follower_accounts"):
            followers_by_group.setdefault(gid, []).append({
                "account": str(fname or ""), "scale": float(scale),
                "replicate": bool(repl), "contract_type": str(ctype or "")})
        groups = []
        for gid, gname, gstatus in cur.execute("SELECT id, name, status FROM groups"):
            lrow = cur.execute("SELECT account_name FROM group_leader_accounts"
                               " WHERE group_id = ?", (gid,)).fetchone()
            groups.append({"group": str(gname or ""), "status": str(gstatus or ""),
                           "leader": str(lrow[0]) if lrow and lrow[0] else "",
                           "followers": sorted(followers_by_group.get(gid, []),
                                               key=lambda f: f["account"])})
        groups.sort(key=lambda g: g["leader"])
    finally:
        con.close()
    return {
        "version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "guard": {"goose_version": int(guard_goose), "user_id": guard_user},
        "accounts": accounts,
        "groups": groups,
    }


# ------------------------------------------------------------- blackout guard

def in_blackout(cfg: dict, now: datetime | None = None) -> bool:
    """True while the market session runs (default 09:30-16:10 ET): the app is
    never quit and the DB never written inside it, whatever the trigger. The
    positions flat-guard still applies outside it."""
    from zoneinfo import ZoneInfo
    et = (now or datetime.now(ZoneInfo("America/New_York")))
    if et.tzinfo is None:
        raise Abort("in_blackout needs an aware datetime")
    hhmm = et.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M")
    return (str(cfg.get("blackout_start", "09:30")) <= hhmm
            <= str(cfg.get("blackout_end", "16:10")))


# ------------------------------------------------------------- run

def _status(result: str, detail: str, plan_date: str, started: str,
            changes: dict | None = None, verify: dict | None = None,
            **extra) -> dict:
    return {"plan_date": plan_date, "started_at": started,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "result": result, "detail": detail,
            "changes": changes or {}, "verify": verify or {}, **extra}


def run(desired_dict: dict, cfg: dict, app: AppController, *,
        force_window: bool = False, dry: bool = False) -> dict:
    """The §7.1 sequence. Returns the §5.2 status dict; never raises for
    operational failures (they become result=aborted/rolled_back)."""
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    db_path = Path(cfg["db_path"])
    plan_date = ""
    lock_path = Path(str(db_path) + ".rabbit-lock")
    lock_fd = None
    try:
        plan_date, desired = parse_desired(desired_dict)

        # ---- preflight, read-only, app may be running
        if not db_path.exists():
            raise Abort(f"DB not found at {db_path}")
        con = open_ro(db_path)
        try:
            st = read_state(con)
        finally:
            con.close()
        ops = resolve(desired, st, cfg["guard_goose"], cfg["guard_user"])
        if ops.empty:
            return _status("noop", "desired state already in place (app untouched)",
                           plan_date, started, ops.summary())
        if dry:
            return _status("planned", "; ".join(describe(ops)), plan_date,
                           started, ops.summary())
        if not force_window and in_blackout(cfg):
            raise Abort(f"market-hours blackout "
                        f"{cfg.get('blackout_start','09:30')}-{cfg.get('blackout_end','16:10')} ET "
                        "- not touching the copier mid-session (--force-window to override)")

        # ---- single-writer lock beside the DB
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(lock_fd, str(os.getpid()).encode())
        except FileExistsError:
            lock_fd = None
            raise Abort(f"another writer holds {lock_path.name} - not re-entering")

        # ---- stop the app
        was_running = app.is_running()
        if was_running:
            app.quit()
        if not wait_exclusive(db_path, app, float(cfg.get("quit_timeout_s", 60))):
            raise Abort("Tradecopia would not quit / release the DB in time - "
                        "leaving everything as-is")

        # ---- backup, then one transaction
        backups_dir = Path(cfg.get("backups_dir", Path(__file__).parent / "backups"))
        backup = make_backup(db_path, backups_dir, plan_date)
        prune_backups(backups_dir, int(cfg.get("backup_keep_days", 30)))
        con = sqlite3.connect(db_path)
        try:
            apply_ops(con, ops, cfg["guard_user"])
        except Exception as exc:
            try:
                con.rollback()
            finally:
                con.close()
            restore_backup(db_path, backup)   # belt and braces (§7.1.4)
            if was_running or cfg.get("start_always"):
                app.start()
            return _status("aborted", f"SQL error mid-transaction: {exc}; "
                           f"backup restored from {backup}", plan_date, started,
                           ops.summary(), backup_dir=str(backup))
        con.close()

        # ---- relaunch + verify
        if was_running or cfg.get("start_always"):
            app.start()
            time.sleep(float(cfg.get("boot_wait_s", 45)))
        ok, why = verify_state(db_path, desired, ops)
        if ok:
            return _status("applied", why, plan_date, started, ops.summary(),
                           {"groups_ok": True, "feeds_rebuilt": True},
                           backup_dir=str(backup))

        # ---- verify failed: quit, restore, relaunch, alert loudly
        if app.is_running():
            app.quit()
            if not wait_exclusive(db_path, app, float(cfg.get("quit_timeout_s", 60))):
                return _status("rolled_back",
                               f"VERIFY FAILED ({why}) and the app will not quit "
                               f"for restore - RESTORE BY HAND from {backup}",
                               plan_date, started, ops.summary(),
                               {"groups_ok": False, "feeds_rebuilt": False},
                               backup_dir=str(backup), restore_failed=True)
        try:
            restore_backup(db_path, backup)
        except Exception as exc:
            return _status("rolled_back",
                           f"VERIFY FAILED ({why}); RESTORE FAILED ({exc}) - "
                           f"restore by hand from {backup}", plan_date, started,
                           ops.summary(), {"groups_ok": False, "feeds_rebuilt": False},
                           backup_dir=str(backup), restore_failed=True)
        if was_running or cfg.get("start_always"):
            app.start()
        return _status("rolled_back", f"verify failed ({why}); backup restored",
                       plan_date, started, ops.summary(),
                       {"groups_ok": False, "feeds_rebuilt": False},
                       backup_dir=str(backup))

    except Abort as exc:
        return _status("aborted", str(exc), plan_date, started)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except OSError:
                pass
