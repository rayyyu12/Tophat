"""TopHat Watchdog -> Discord webhook (embeds).

Both modes are ONE-SHOT: Task Scheduler starts the script, it does its reads
(one read-only SQLite open + two localhost HTTP GETs + one webhook POST),
posts, and exits. No polling, no resident process, no server load.

  (default)  pre-market check (Tradecopia connections/feeds by FIRM, app
             running, TopHat server up + armed). Posts ONLY when something
             needs action — set "send_green": true to also get an all-clear.
  --recap    late-morning digest, always posts: drive direction, today's
             reconciled outcomes (target/stop per account), accounts now
             payout-ready, connection drops today (only if any).

Config: deploy/watchdog_config.json
    {"webhook_url": "https://discord.com/api/webhooks/...",
     "tophat_url": "http://127.0.0.1:8800",
     "settings_json": "<abs path to data/users/<uid>/settings.json>",
     "tradecopia_db": "<abs path to tradecopia-desktop.db>",
     "send_green": false}
The recap reads trade_log.jsonl / account_states.json / mirrors.json from the
settings_json directory — works even when the TopHat server is down.

Schedule (LOCAL clock; 08:00+08:25 CT = 09:00/09:25 ET — the second run exists
because the 2026-07-07 Topstep kick landed at 08:14 CT, after an 08:00 check):
    schtasks /Create /TN "TopHat Watchdog 0800" /SC WEEKLY /D MON,TUE,WED,THU,FRI ^
      /ST 08:00 /TR "\"<venv python.exe>\" \"<this file>\""
    (same at 08:25, and at 10:00 with --recap)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "watchdog_config.json"

GREEN, AMBER, RED = 0x2ECC71, 0xE67E22, 0xED4245

DEFAULTS = {
    "webhook_url": "",
    "tophat_url": "http://127.0.0.1:8800",
    "settings_json": r"C:\Users\Rayyan Khan\Desktop\Project TopHat\data\users\1\settings.json",
    "tradecopia_db": r"C:\Users\Rayyan Khan\AppData\Roaming\Tradecopia\tradecopia-desktop.db",
    "send_green": False,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    return cfg


def firm_label(org: str) -> str:
    """entities.organization -> short display name."""
    o = (org or "").lower()
    if "apex" in o:
        return "Apex"
    if "topstep" in o:
        return "Topstep"
    if "lucid" in o:
        return "Lucid"
    if "tradeify" in o:
        return "Tradeify"
    return org or "?"


# ---------------------------------------------------------------- tradecopia

def read_tradecopia(db_path: str) -> dict:
    """One read-only pass: firm connection status, per-firm feed health,
    drops today, and the account id -> name map (for recap display)."""
    out = {"app_running": False, "db_ok": False, "conns": [], "feeds_bad": {},
           "drops_today": [], "names": {}}
    tl = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Tradecopia.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=15).stdout
    out["app_running"] = "Tradecopia.exe" in tl
    if not Path(db_path).exists():
        return out
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        for eid, org, connected, disc_at in cur.execute(
                "SELECT id, organization, is_connected, disconnected_at FROM entities"):
            firm = firm_label(org)
            out["conns"].append({"firm": firm, "up": bool(connected),
                                 "since": str(disc_at or "")[:16]})
            if disc_at and str(disc_at)[:10] == today:
                state = "reconnected" if connected else "**still down**"
                out["drops_today"].append(
                    f"{firm} dropped {str(disc_at)[11:16]} local — {state}")
        # per-firm feed health, via feeds.entity_id -> entities.organization
        for org, n_all, n_bad in cur.execute(
                "SELECT e.organization, COUNT(*), SUM(f.connection_status != 'connected')"
                " FROM feeds f JOIN entities e ON e.id = f.entity_id"
                " GROUP BY e.organization"):
            if n_bad:
                out["feeds_bad"][firm_label(org)] = (n_bad, n_all)
        out["names"] = {int(i): n for i, n in cur.execute("SELECT id, name FROM accounts")}
        out["db_ok"] = True
    finally:
        con.close()
    return out


# ---------------------------------------------------------------- tophat

def check_tophat(url: str, settings_json: str) -> tuple[list[str], list[str]]:
    ok: list[str] = []
    bad: list[str] = []
    try:
        urllib.request.urlopen(url, timeout=5)
        ok.append("server up")
    except urllib.error.HTTPError:
        ok.append("server up")   # any HTTP response proves it's listening
    except Exception:
        bad.append(f"server unreachable at {url}")
    try:
        settings = json.loads(Path(settings_json).read_text(encoding="utf-8"))
        if settings.get("auto_execute", False):
            ok.append("armed (auto-execute ON)")
        else:
            bad.append("auto_execute is OFF — nothing fires today")
    except Exception as exc:
        bad.append(f"could not read settings: {exc}")
    return ok, bad


def tophat_drive(url: str) -> str:
    """Cache-only unauthenticated /api/healthz; never raises."""
    try:
        with urllib.request.urlopen(f"{url}/api/healthz", timeout=5) as r:
            s = json.loads(r.read().decode())
        side = (s.get("drive") or "").upper()
        src = s.get("drive_source") or ""
        if side:
            return f"{side} ({src})" if src else side
        return "not locked (flat open, or no read since 09:45 ET)"
    except Exception:
        return "not available"


def user_data(settings_json: str) -> Path:
    return Path(settings_json).parent


def account_labels(data_dir: Path, tc_names: dict[int, str]) -> dict[int, str]:
    """Best display name per account id: registry alias > Tradecopia accounts
    table > TopHat's account_names.json write-through cache > #id (caller)."""
    labels: dict[int, str] = {}
    try:
        cached = json.loads((data_dir / "account_names.json").read_text(encoding="utf-8"))
        labels.update({int(k): v for k, v in cached.items() if v})
    except Exception:
        pass
    labels.update(tc_names)
    try:
        reg = json.loads((data_dir / "account_registry.json").read_text(encoding="utf-8"))
        for k, e in (reg.get("accounts") or {}).items():
            if isinstance(e, dict) and e.get("alias"):
                labels[int(k)] = e["alias"]
    except Exception:
        pass
    return labels


def todays_outcomes(data_dir: Path, names: dict[int, str]) -> dict[str, list[str]]:
    """Reconciled trades from trade_log.jsonl with trade_date == today (ET dates
    are what the log stamps)."""
    log = data_dir / "trade_log.jsonl"
    groups: dict[str, list[str]] = {"win": [], "loss": [], "flat": []}
    if not log.exists():
        return groups
    today = datetime.now().strftime("%Y-%m-%d")
    for ln in log.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "trade" or (e.get("trade_date") or e.get("date")) != today:
            continue
        aid = e.get("account_id")
        name = names.get(aid) or f"#{aid}"
        pnl = float(e.get("pnl") or 0.0)
        groups.setdefault(e.get("outcome") or "flat", []).append(
            f"{name} · {e.get('label', '?')} ({pnl:+,.0f})")
    return groups


def open_and_ready(data_dir: Path, names: dict[int, str]) -> tuple[list[str], list[str]]:
    """(still-open today, payout-ready) from account_states.json + mirrors.json."""
    still_open: list[str] = []
    ready: list[str] = []
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        states = json.loads((data_dir / "account_states.json").read_text(encoding="utf-8"))
    except Exception:
        states = {}
    for aid, st in states.items():
        name = names.get(int(aid)) or f"#{aid}"
        if st.get("pending_label") and st.get("pending_date") == today:
            still_open.append(f"{name} ({st['pending_label']})")
        if st.get("payout_ready"):
            ready.append(name)
    try:
        mirrors = json.loads((data_dir / "mirrors.json").read_text(encoding="utf-8"))
    except Exception:
        mirrors = {}
    for mid, m in (mirrors or {}).items():
        if isinstance(m, dict) and m.get("payout_ready"):
            ready.append(m.get("alias") or mid)
    return still_open, ready


# ---------------------------------------------------------------- discord

def send_embed(webhook_url: str, title: str, color: int, fields: list[dict],
               description: str = "") -> None:
    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": "TopHat Watchdog"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if description:
        embed["description"] = description
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps({"embeds": [embed]}).encode(),
        headers={"Content-Type": "application/json",
                 # Cloudflare 403s the default Python-urllib UA
                 "User-Agent": "TopHatWatchdog/1.0"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"discord {exc.code}: {body}") from None


# ---------------------------------------------------------------- modes

def run_premarket(cfg: dict) -> tuple[str, int, list[dict], str] | None:
    """None = all clear and send_green is off (post nothing)."""
    tc = read_tradecopia(cfg["tradecopia_db"])
    th_ok, th_bad = check_tophat(cfg["tophat_url"], cfg["settings_json"])

    copier_bad: list[str] = []
    if not tc["app_running"]:
        copier_bad.append("Tradecopia is NOT RUNNING")
    if not tc["db_ok"]:
        copier_bad.append("Tradecopia DB unreadable")
    for c in tc["conns"]:
        if not c["up"]:
            copier_bad.append(f"{c['firm']} connection DOWN since {c['since']}")
    for firm, (n_bad, n_all) in sorted(tc["feeds_bad"].items()):
        copier_bad.append(f"{firm} feeds not connected ({n_bad}/{n_all})")

    n_bad = len(copier_bad) + len(th_bad)
    if n_bad == 0 and not cfg.get("send_green"):
        return None
    copier_ok = []
    if tc["app_running"]:
        copier_ok.append("app running")
    up = [c["firm"] for c in tc["conns"] if c["up"]]
    if up and not any(not c["up"] for c in tc["conns"]):
        copier_ok.append(f"connections up: {', '.join(sorted(set(up)))}")
    fields = [
        {"name": "Copier · Tradecopia",
         "value": "\n".join([f"❌ {b}" for b in copier_bad]
                            + [f"✅ {o}" for o in copier_ok]) or "—", "inline": True},
        {"name": "Trader · TopHat",
         "value": "\n".join([f"❌ {b}" for b in th_bad]
                            + [f"✅ {o}" for o in th_ok]) or "—", "inline": True},
    ]
    if n_bad:
        return "Pre-market warning", RED, fields, "Action needed before the open."
    return "Pre-market all clear", GREEN, fields, ""


def run_recap(cfg: dict) -> tuple[str, int, list[dict], str]:
    tc = read_tradecopia(cfg["tradecopia_db"])
    data_dir = user_data(cfg["settings_json"])
    drive = tophat_drive(cfg["tophat_url"])
    names = account_labels(data_dir, tc["names"])
    outcomes = todays_outcomes(data_dir, names)
    still_open, ready = open_and_ready(data_dir, names)

    fields = [{"name": "Drive",
               "value": ("📈 " if "LONG" in drive else
                         "📉 " if "SHORT" in drive else "▪️ ") + drive,
               "inline": False}]
    res_lines = ([f"🎯 hit target: {t}" for t in outcomes.get("win", [])]
                 + [f"🛑 hit stop: {t}" for t in outcomes.get("loss", [])]
                 + [f"➖ flat: {t}" for t in outcomes.get("flat", [])])
    fields.append({"name": "Results",
                   "value": "\n".join(res_lines) or "no reconciled trades yet",
                   "inline": False})
    if still_open:
        fields.append({"name": "Still open",
                       "value": "\n".join(f"⏳ {s}" for s in still_open), "inline": False})
    if ready:
        fields.append({"name": "Payout ready",
                       "value": "\n".join(f"💰 {r}" for r in ready), "inline": False})
    if tc["drops_today"]:
        fields.append({"name": "Connection drops today",
                       "value": "\n".join(f"⚠️ {d}" for d in tc["drops_today"]),
                       "inline": False})
    still_down = any(not c["up"] for c in tc["conns"])
    color = RED if still_down else (AMBER if tc["drops_today"] else GREEN)
    return "Morning recap", color, fields, ""


def main() -> int:
    cfg = load_config()
    recap = "--recap" in sys.argv[1:]
    result = run_recap(cfg) if recap else run_premarket(cfg)
    if result is None:
        print("all clear — nothing posted (send_green is off)")
        return 0
    title, color, fields, desc = result
    plain = f"{title}: " + " | ".join(
        f"{f['name']}: {f['value']}" for f in fields).replace("\n", "; ")
    # cp1252 consoles can't render the emoji; the Discord payload stays UTF-8
    enc = sys.stdout.encoding or "utf-8"
    print(plain.encode(enc, "replace").decode(enc))
    if not cfg["webhook_url"]:
        print("(no webhook_url configured — printed only)")
        return 1 if color == RED else 0
    try:
        send_embed(cfg["webhook_url"], title, color, fields, desc)
    except Exception as exc:
        print(f"discord send failed: {exc}")
        return 2
    return 1 if color == RED else 0


if __name__ == "__main__":
    sys.exit(main())
