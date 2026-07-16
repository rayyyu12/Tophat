"""TopHat Rabbit — the copier-box bridge service.

Runs 24/7 on the Windows machine that runs Tradecopia. It only ever dials
OUT — the box needs no inbound port. Four jobs:

1. **Apply** (forward sync): pull the desired copier state from TopHat and
   write it into the Tradecopia DB with tc_apply (quit -> backup -> write ->
   relaunch -> verify -> rollback on failure).
2. **Observe** (reverse sync, plan doc §13): SELECT-only read of follower
   balances/names out of the same DB, POSTed to TopHat after every cycle.
3. **Report**: push the apply status back; TopHat fans it to Discord.
4. **Heartbeat**: POST a read-only Tradecopia health check (app up, per-firm
   connections, feeds) at startup and every ~5 min — it feeds each user's
   server-side premarket/recap Discord notices (TopHat can't dial in).

Cadence (operator-designed 2026-07-08): no continuous heavy polling. One
scheduled apply per day at `apply_at` ET (default 22:30 — after activations
and purchases are done, and clear of TopHat's 22:00 OCO probe so the two
never share a log minute), with retries every `retry_every_min` when TopHat
isn't ready. In between, a ~30-byte flag poll every `poll_interval_s` keeps a
"Sync now" click in TopHat (or a Mark-applied auto-flag) landing within a
minute. Writes always refuse during the market-hours blackout. If every
retry fails, the give-up is LOUD: a "gave-up" status is pushed to TopHat,
which fans it to Discord — a silent skip would leave Tradecopia running
yesterday's mapping all the next day.

    python rabbit.py run                    # the 24/7 service loop
    python rabbit.py once [--force-window]  # one pull->apply->observe, then exit
    python rabbit.py plan                   # pull + print the diff, touch nothing
    python rabbit.py observe                # reverse sync only (read-only, any time)
    python rabbit.py reconnect              # quit + relaunch the app, NO db write
    python rabbit.py rollback --backup DIR  # manual restore from a backup dir
    python rabbit.py status                 # print the last apply status

Config: rabbit_config.json next to this file (see rabbit_config.json.example).
Requires Python 3.11+ and `pip install tzdata` on Windows (ET schedule math).
State: rabbit_state.json (schedule/retry bookkeeping), rabbit.log (append log).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import tc_apply                    # run as a script from rabbit/
except ImportError:                    # imported as the rabbit package (tests)
    from rabbit import tc_apply

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "rabbit_config.json"
STATE_FILE = HERE / "rabbit_state.json"
LOG_FILE = HERE / "rabbit.log"
ET = ZoneInfo("America/New_York")

REQUIRED_CFG = ("tophat_url", "box_token", "db_path", "exe",
                "guard_goose", "guard_user")


def log(msg: str) -> None:
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f"no {CONFIG_FILE.name} - copy rabbit_config.json.example and fill it in")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_CFG if not cfg.get(k)]
    if missing:
        sys.exit(f"rabbit_config.json missing required keys: {missing}")
    return cfg


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ------------------------------------------------------------- tophat http

def _request(cfg: dict, path: str, payload: dict | None = None):
    req = urllib.request.Request(
        cfg["tophat_url"].rstrip("/") + path,
        data=(json.dumps(payload).encode() if payload is not None else None),
        headers={"Authorization": f"Bearer {cfg['box_token']}",
                 "Content-Type": "application/json",
                 "User-Agent": "TopHatRabbit/1.0"})
    return urllib.request.urlopen(req, timeout=30)


def poll_flag(cfg: dict) -> dict | None:
    """The cheap heartbeat: {'sync_requested': bool}. None on any failure.
    Carries the box's apply_at so the Operations page can show the nightly
    schedule (a box-config fact TopHat cannot otherwise know)."""
    apply_at = urllib.parse.quote(str(cfg.get("apply_at", "22:30")))
    try:
        with _request(cfg, f"/api/ops/tc-poll?apply_at={apply_at}") as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def pull_desired(cfg: dict) -> tuple[dict | None, str]:
    """(payload, '') or (None, reason). A 409 reason is TopHat saying 'not
    ready' (plan unapplied / data incomplete) — retryable by design."""
    try:
        with _request(cfg, "/api/ops/tc-desired") as r:
            return json.loads(r.read().decode()), ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        try:
            reason = json.loads(body).get("error", body)
        except ValueError:
            reason = body or str(exc)
        if exc.code == 401:
            reason = "TOKEN REJECTED - re-pair this box in TopHat Settings"
        return None, f"{exc.code}: {reason}"
    except Exception as exc:
        return None, f"unreachable: {exc}"


def push_status(cfg: dict, status: dict) -> None:
    try:
        with _request(cfg, "/api/ops/tc-status", status):
            pass
        log(f"status posted: {status['result']}")
    except Exception as exc:
        log(f"could not post status (kept locally): {exc}")
    (HERE / "tc_apply_status.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8")


def tc_health(cfg: dict) -> dict:
    """Read-only Tradecopia health for the TopHat heartbeat: app process up,
    per-firm connection status, feed health, drops today. Replaces the retired
    deploy/watchdog.py checks — SELECT-only, safe while the app is running."""
    out: dict = {"generated_at": datetime.now(ET).isoformat(timespec="seconds"),
                 "app_running": False, "db_ok": False, "conns": [],
                 "feeds_bad": {}, "drops_today": []}
    try:
        tl = subprocess.run(
            ["tasklist", "/FI",
             f"IMAGENAME eq {cfg.get('process_name', 'Tradecopia.exe')}",
             "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15).stdout
        out["app_running"] = cfg.get("process_name", "Tradecopia.exe") in tl
    except Exception:
        pass

    def firm(org: str) -> str:
        o = (org or "").lower()
        for key, label in (("apex", "Apex"), ("topstep", "Topstep"),
                           ("lucid", "Lucid"), ("tradeify", "Tradeify")):
            if key in o:
                return label
        return org or "?"

    try:
        con = sqlite3.connect(f"file:{cfg['db_path']}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            today = datetime.now().strftime("%Y-%m-%d")   # DB stamps local time
            for org, up, disc in cur.execute(
                    "SELECT organization, is_connected, disconnected_at FROM entities"):
                out["conns"].append({"firm": firm(org), "up": bool(up),
                                     "since": str(disc or "")[:16]})
                if disc and str(disc)[:10] == today:
                    state = "reconnected" if up else "still down"
                    out["drops_today"].append(
                        f"{firm(org)} dropped {str(disc)[11:16]} local — {state}")
            for org, n_all, n_bad in cur.execute(
                    "SELECT e.organization, COUNT(*),"
                    " SUM(f.connection_status != 'connected')"
                    " FROM feeds f JOIN entities e ON e.id = f.entity_id"
                    " GROUP BY e.organization"):
                if n_bad:
                    out["feeds_bad"][firm(org)] = [int(n_bad), int(n_all)]
            out["db_ok"] = True
        finally:
            con.close()
    except Exception:
        pass
    return out


def push_heartbeat(cfg: dict) -> None:
    """POST the TC health read to TopHat (feeds the per-user premarket/recap
    Discord notifications). Best-effort — a miss just ages the last report."""
    try:
        with _request(cfg, "/api/ops/tc-heartbeat", tc_health(cfg)):
            pass
    except Exception as exc:
        log(f"heartbeat post failed: {exc}")


def push_observed(cfg: dict) -> str:
    """Reverse sync: read the DB (SELECT-only) and POST it. Returns a short
    human summary for the status line ('' when skipped/failed)."""
    try:
        payload = tc_apply.read_observed(
            cfg["db_path"], cfg["guard_goose"], cfg["guard_user"],
            include_leader_entities=bool(cfg.get("observe_leader_entities")))
    except tc_apply.Abort as exc:
        log(f"observe guard tripped: {exc}")
        return f"observe refused ({exc})"
    except Exception as exc:
        log(f"observe read failed: {exc}")
        return ""
    try:
        with _request(cfg, "/api/ops/tc-observed", payload) as r:
            resp = json.loads(r.read().decode())
        s = resp.get("summary") or {}
        line = (f"observed {len(payload['accounts'])}: "
                f"booked {s.get('booked', 0)}, stale {s.get('stale', 0)}, "
                f"proposed {s.get('proposed', 0)}")
        log(line)
        return line
    except Exception as exc:
        log(f"observe post failed: {exc}")
        return ""


# ------------------------------------------------------------- cycles

def full_cycle(cfg: dict, app: tc_apply.AppController, trigger: str, *,
               force_window: bool = False) -> dict | None:
    """One pull -> apply -> observe -> report pass. Returns the status dict,
    or None when the pull failed (caller decides whether to retry)."""
    desired, why = pull_desired(cfg)
    if desired is None:
        log(f"pull failed ({trigger}): {why}")
        # a refused pull must not blind the server: without this, deleting the
        # last mirror 409s every pull and the observe that would surface the
        # replacement accounts never runs (2026-07-16 lockout)
        observed_line = push_observed(cfg)
        if trigger == "manual":
            # the operator clicked Sync now - tell them why nothing happened
            detail = f"sync requested, but pull failed - {why}"
            if observed_line:
                detail = f"{detail} | {observed_line}"
            push_status(cfg, {"plan_date": "", "result": "aborted",
                              "detail": detail,
                              "changes": {}, "verify": {}})
        elif trigger in ("scheduled", "retry"):
            # remembered so the give-up alert can say WHY the night failed
            state = load_state()
            state["last_pull_error"] = why
            save_state(state)
        return None
    log(f"applying plan {desired.get('plan_date')} ({trigger}, "
        f"{len(desired.get('groups') or [])} group(s))")
    status = tc_apply.run(desired, cfg, app, force_window=force_window)
    log(f"{status['result']}: {status['detail']}")
    observed_line = push_observed(cfg)
    if observed_line:
        status["detail"] = f"{status['detail']} | {observed_line}"
        status["observed"] = observed_line
    push_status(cfg, status)
    return status


def _scheduled_due(cfg: dict, state: dict, now_et: datetime) -> bool:
    apply_at = str(cfg.get("apply_at", "22:30"))
    today = now_et.strftime("%Y-%m-%d")
    return (now_et.strftime("%H:%M") >= apply_at
            and state.get("last_scheduled_date") != today)


def _retry_due(state: dict, now_et: datetime) -> bool:
    nxt = state.get("retry_next_at", "")
    return bool(nxt) and now_et.isoformat() >= nxt


def _arm_retry(cfg: dict, state: dict, now_et: datetime) -> None:
    count = int(state.get("retry_count", 0)) + 1
    if count > int(cfg.get("max_retries", 8)):
        why = state.pop("last_pull_error", "")
        log(f"giving up after {count - 1} retries - will try again at the next "
            "scheduled time or Sync now")
        state.pop("retry_next_at", None)
        state["retry_count"] = 0
        # Loud give-up (operator decision 2026-07-09): push a status so TopHat
        # fans it to Discord — Tradecopia is now running YESTERDAY'S mapping
        # until the next scheduled apply or a manual Sync now.
        push_status(cfg, {
            "plan_date": "", "result": "gave-up",
            "detail": (f"nightly apply abandoned after {count - 1} retries - "
                       f"last error: {why or 'TopHat not ready'}. Tradecopia "
                       "keeps yesterday's mapping until the next scheduled "
                       "apply or Sync now."),
            "changes": {}, "verify": {}})
        return
    nxt = now_et + timedelta(minutes=float(cfg.get("retry_every_min", 15)))
    state["retry_count"] = count
    state["retry_next_at"] = nxt.isoformat()
    log(f"retry {count} armed for {nxt.strftime('%H:%M ET')}")


def _disarm_retry(state: dict) -> None:
    state.pop("retry_next_at", None)
    state.pop("last_pull_error", None)
    state["retry_count"] = 0


def run_loop(cfg: dict, app: tc_apply.AppController) -> None:
    interval = float(cfg.get("poll_interval_s", 60))
    hb_every = float(cfg.get("heartbeat_every_s", 300))
    last_hb = 0.0     # monotonic; 0 = post one immediately at startup
    log(f"rabbit awake - apply daily at {cfg.get('apply_at', '22:30')} ET, "
        f"flag-poll every {interval:.0f}s, TC heartbeat every {hb_every:.0f}s "
        f"against {cfg['tophat_url']}")
    while True:
        try:
            now_et = datetime.now(ET)
            state = load_state()
            if last_hb == 0.0 or time.monotonic() - last_hb >= hb_every:
                push_heartbeat(cfg)   # TC health -> per-user Discord notices
                last_hb = time.monotonic()
            flag = poll_flag(cfg)   # cheap; doubles as the box heartbeat
            trigger = ""
            if flag and flag.get("sync_requested"):
                trigger = "manual"
            elif _scheduled_due(cfg, state, now_et):
                trigger = "scheduled"
                state["last_scheduled_date"] = now_et.strftime("%Y-%m-%d")
                _disarm_retry(state)
                save_state(state)
            elif _retry_due(state, now_et):
                trigger = "retry"
            if trigger:
                status = full_cycle(cfg, app, trigger)
                state = load_state()
                if status is None and trigger in ("scheduled", "retry"):
                    _arm_retry(cfg, state, now_et)      # TopHat not ready yet
                elif status is not None:
                    _disarm_retry(state)
                save_state(state)
        except Exception as exc:       # the loop must survive anything
            log(f"cycle crashed (loop continues): {exc!r}")
        time.sleep(interval)


def reconnect(cfg: dict, app: tc_apply.AppController) -> int:
    """§11.3: quit -> relaunch, no DB write - heals a dead Topstep connection
    (and Tradovate too, if the auto-login hypothesis holds at Gate A)."""
    if app.is_running():
        log("quitting Tradecopia (graceful)")
        app.quit()
        if not tc_apply.wait_exclusive(Path(cfg["db_path"]), app,
                                       float(cfg.get("quit_timeout_s", 60))):
            log("app would not quit - aborting reconnect")
            return 1
    log("starting Tradecopia")
    app.start()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=["run", "once", "plan", "observe",
                                     "reconnect", "rollback", "status"])
    ap.add_argument("--force-window", action="store_true",
                    help="write during the market-hours blackout (supervised runs)")
    ap.add_argument("--backup", help="backup dir for rollback mode")
    args = ap.parse_args()

    cfg = load_config()
    app = tc_apply.AppController(cfg["exe"], cfg.get("process_name", "Tradecopia.exe"))

    if args.mode == "run":
        run_loop(cfg, app)              # never returns
        return 0
    if args.mode == "once":
        status = full_cycle(cfg, app, "once", force_window=args.force_window)
        print(json.dumps(status, indent=2) if status else "pull failed - see log")
        return 0 if (status and status["result"] in ("applied", "noop")) else 1
    if args.mode == "plan":
        desired, why = pull_desired(cfg)
        if desired is None:
            print(f"pull failed: {why}")
            return 1
        print(json.dumps(tc_apply.run(desired, cfg, app, dry=True), indent=2))
        return 0
    if args.mode == "observe":
        line = push_observed(cfg)
        print(line or "observe failed - see log")
        return 0 if line else 1
    if args.mode == "reconnect":
        return reconnect(cfg, app)
    if args.mode == "rollback":
        if not args.backup:
            sys.exit("rollback needs --backup <dir>")
        if app.is_running():
            sys.exit("quit Tradecopia first, then re-run rollback")
        tc_apply.restore_backup(Path(cfg["db_path"]), Path(args.backup))
        log(f"restored DB from {args.backup} - start Tradecopia when ready")
        return 0
    if args.mode == "status":
        p = HERE / "tc_apply_status.json"
        print(p.read_text(encoding="utf-8") if p.exists() else "no status yet")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
