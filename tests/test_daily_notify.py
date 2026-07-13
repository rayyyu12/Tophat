"""Server-side per-user premarket/recap Discord notices + Rabbit TC heartbeat.

Replaced deploy/watchdog.py (retired 2026-07-14): each user gets their own
webhook notifications from the automation loop; Tradecopia health arrives via
the box heartbeat and is omitted entirely for users with no paired box.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tophat.services import daily_notify, notify
from tophat.store import boxes
from tophat.store.config import load_settings, save_settings

ET = ZoneInfo("America/New_York")
WED_0905 = datetime(2026, 7, 1, 9, 5, tzinfo=ET)
WED_1105 = datetime(2026, 7, 1, 11, 5, tzinfo=ET)
SAT_0905 = datetime(2026, 7, 4, 9, 5, tzinfo=ET)
HOOK = "https://discord.com/api/webhooks/1/test"


@pytest.fixture
def posts(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(notify, "post_discord",
                        lambda url, title, **kw: sent.append((url, title, kw)))
    return sent


def _hook(**kw):
    s = load_settings()
    s.discord_webhook_url = HOOK
    for k, v in kw.items():
        setattr(s, k, v)
    save_settings(s)


def test_premarket_warns_when_disarmed_once(posts):
    _hook(auto_execute=False)
    daily_notify.maybe_send(1, WED_0905)
    assert len(posts) == 1
    url, title, kw = posts[0]
    assert url == HOOK and title == "Pre-market warning"
    txt = str(kw["fields"])
    assert "auto_execute is OFF" in txt
    assert "Tradecopia" not in txt          # no paired box -> no copier section
    daily_notify.maybe_send(1, WED_0905)    # same day: stamped, silent
    assert len(posts) == 1


def test_premarket_silent_when_all_clear(posts):
    _hook(auto_execute=True)
    daily_notify.maybe_send(1, WED_0905)
    assert posts == []                      # silence is the green signal
    daily_notify.maybe_send(1, WED_1105)    # ...but the recap still posts
    assert [t for _, t, _ in posts] == ["Morning recap"]


def test_premarket_flags_silent_copier_box(posts):
    _hook(auto_execute=True)
    boxes.create_box(1, "desk pc")          # paired but never reported
    daily_notify.maybe_send(1, WED_0905)
    assert len(posts) == 1
    assert "hasn't reported" in str(posts[0][2]["fields"])


def test_premarket_flags_downed_connection(posts):
    _hook(auto_execute=True)
    row, _tok = boxes.create_box(1, "desk pc")
    boxes.record_heartbeat(row["box_id"], {
        "app_running": True, "db_ok": True,
        "conns": [{"firm": "Lucid", "up": False, "since": "2026-07-01 08:1"},
                  {"firm": "Topstep", "up": True, "since": ""}],
        "feeds_bad": {}, "drops_today": []})
    daily_notify.maybe_send(1, WED_0905)
    assert len(posts) == 1
    assert "Lucid connection DOWN" in str(posts[0][2]["fields"])


def test_recap_includes_todays_outcomes(posts):
    from tophat.clock import trading_day
    from tophat.store import trade_log
    _hook(auto_execute=True)
    today = trading_day(datetime.now(ET))   # ops_recap keys off the real clock
    trade_log.log_event("trade", account_id=7000001, trade_date=today,
                        label="nuke", outcome="win", pnl=3192.44,
                        balance=3192.44, phase="funded")
    daily_notify.maybe_send(1, WED_1105)
    assert len(posts) == 1
    _, title, kw = posts[0]
    assert title == "Morning recap"
    txt = str(kw["fields"])
    assert "hit target" in txt and "nuke" in txt


def test_quiet_hours_and_weekends(posts):
    _hook(auto_execute=False)
    daily_notify.maybe_send(1, datetime(2026, 7, 1, 8, 30, tzinfo=ET))
    daily_notify.maybe_send(1, datetime(2026, 7, 1, 10, 0, tzinfo=ET))
    daily_notify.maybe_send(1, SAT_0905)
    assert posts == []


def test_no_webhook_no_post_no_stamp(posts):
    # Without a webhook nothing happens - and nothing is STAMPED, so pasting
    # the webhook later the same morning still gets the premarket warning.
    s = load_settings()
    s.auto_execute = False
    save_settings(s)
    daily_notify.maybe_send(1, WED_0905)
    assert posts == []
    _hook(auto_execute=False)
    daily_notify.maybe_send(1, WED_0905)
    assert len(posts) == 1


def test_heartbeat_endpoint_roundtrip(client):
    row, token = boxes.create_box(1, "desk pc")
    r = client.post("/api/ops/tc-heartbeat", json={"app_running": True},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["ok"] is True
    hb = boxes.latest_heartbeat(1)
    assert hb is not None
    assert hb[0]["app_running"] is True and hb[1] < 60
    assert client.post("/api/ops/tc-heartbeat", json={}).status_code == 401
